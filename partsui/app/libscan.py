"""Find symbol and footprint names in .SchLib / .PcbLib files (optional, never fatal).

Both are OLE compound files:
  * SchLib: the FileHeader stream is `|KEY=VALUE|...` text with LibRef<n>=<name> per symbol
    (keys are upper- or mixed-case depending on the Altium version; %UTF8%LibRef<n> holds
    the UTF-8 spelling when the name isn't plain ANSI).
  * PcbLib: each footprint is a top-level storage. Storage names are cut at 31 characters,
    so the full name comes from the footprint's Parameters stream (|PATTERN=<name>|) or,
    failing that, from the name list in Library/Data.
"""
from __future__ import annotations

import bisect
import logging
import os
import re
import struct
import threading
import time
from dataclasses import dataclass, field

import olefile

log = logging.getLogger("partsui.libscan")

PCB_SYSTEM_STORAGES = {"fileheader", "fileversioninfo", "library", "sectionkeys", "storage",
                       "additional", "header"}
MAX_FILE_SIZE = 256 * 1024 * 1024


@dataclass(frozen=True)
class LibEntry:
    name: str
    path: str          # relative to its root, forward slashes
    root: str
    description: str = ""


def _records(data: bytes) -> dict[str, str]:
    """|KEY=VALUE|... -> {KEY (upper-case): value}. Text is Windows-1252/latin-1."""
    text = data.decode("latin-1")
    out: dict[str, str] = {}
    for part in text.split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.rstrip("\x00\r\n")
    return out


def _utf8(rec: dict[str, str], key: str) -> str | None:
    raw = rec.get("%UTF8%" + key)
    if raw is None:
        return None
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return None


def parse_schlib(path: str) -> list[tuple[str, str]]:
    """[(symbol name, description)]"""
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("FileHeader"):
            raise ValueError("no FileHeader stream")
        rec = _records(ole.openstream("FileHeader").read())
    names = []
    for key in rec:
        m = re.fullmatch(r"LIBREF(\d+)", key)
        if not m:
            continue
        n = m.group(1)
        name = _utf8(rec, key) or rec[key]
        descr = _utf8(rec, f"COMPDESCR{n}") or rec.get(f"COMPDESCR{n}", "")
        if name.strip():
            names.append((int(n), name.strip(), descr.strip()))
    return [(name, descr) for _, name, descr in sorted(names)]


def _library_data_names(ole: olefile.OleFileIO) -> list[str]:
    """Footprint names listed after the header in Library/Data:
    u32 header length, header, u32 count, then per name: u32 len, u8 len, bytes."""
    if not ole.exists("Library/Data"):
        return []
    d = ole.openstream("Library/Data").read()
    pos = 4 + struct.unpack_from("<I", d, 0)[0]
    count = struct.unpack_from("<I", d, pos)[0]
    pos += 4
    names = []
    for _ in range(min(count, 100000)):
        rec_len = struct.unpack_from("<I", d, pos)[0]
        strlen = d[pos + 4]
        names.append(d[pos + 5: pos + 5 + strlen].decode("latin-1"))
        pos += 4 + rec_len
    return names


def parse_pcblib(path: str) -> list[tuple[str, str]]:
    """[(footprint name, description)]"""
    with olefile.OleFileIO(path) as ole:
        storages = sorted({e[0] for e in ole.listdir(streams=False, storages=True)})
        found: list[tuple[str, str]] = []
        for st in storages:
            if st.lower() in PCB_SYSTEM_STORAGES or not ole.exists(f"{st}/Data"):
                continue
            name, descr = st, ""
            if ole.exists(f"{st}/Parameters"):
                rec = _records(ole.openstream(f"{st}/Parameters").read())
                name = _utf8(rec, "PATTERN") or rec.get("PATTERN") or st
                descr = _utf8(rec, "DESCRIPTION") or rec.get("DESCRIPTION", "")
            found.append((name.strip(), descr.strip()))
        if not found or any(len(n) == 31 for n, _ in found):
            # Storage names may be truncated; Library/Data has the full list.
            try:
                listed = _library_data_names(ole)
            except (struct.error, IndexError, OSError):
                listed = []
            if listed:
                have = {n for n, _ in found}
                for n in listed:
                    if n not in have and not any(n.startswith(h) and len(h) == 31 for h in have):
                        found.append((n, ""))
        return found


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


@dataclass
class LibIndex:
    sch: list[LibEntry] = field(default_factory=list)
    pcb: list[LibEntry] = field(default_factory=list)
    files: int = 0
    failed: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    missing_roots: list[str] = field(default_factory=list)
    scanned_at: float | None = None
    seconds: float = 0.0
    _keys: dict = field(default_factory=dict, repr=False)

    def finish(self) -> "LibIndex":
        for kind in ("sch", "pcb"):
            items = sorted(getattr(self, kind), key=lambda e: (e.name.casefold(), e.path.casefold()))
            setattr(self, kind, items)
            self._keys[kind] = [e.name.casefold() for e in items]
        return self

    def suggest(self, kind: str, q: str, limit: int = 20) -> list[LibEntry]:
        """Prefix matches first (binary search on the sorted names), then contains-matches."""
        items: list[LibEntry] = getattr(self, kind, [])
        keys: list[str] = self._keys.get(kind, [])
        q = q.strip().casefold()
        if not q:
            return items[:limit]
        out = []
        i = bisect.bisect_left(keys, q)
        while i < len(keys) and keys[i].startswith(q) and len(out) < limit:
            out.append(items[i])
            i += 1
        if len(out) < limit:
            for k, e in zip(keys, items):
                if q in k and not k.startswith(q):
                    out.append(e)
                    if len(out) >= limit:
                        break
        return out

    @property
    def count(self) -> int:
        return len(self.sch) + len(self.pcb)


def scan(dirs: list[str] | tuple[str, ...]) -> LibIndex:
    t0 = time.monotonic()
    idx = LibIndex()
    for root in dirs:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            idx.missing_roots.append(root)
            continue
        idx.roots.append(root)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                low = fn.lower()
                if low.endswith(".schlib"):
                    kind, parser = "sch", parse_schlib
                elif low.endswith(".pcblib"):
                    kind, parser = "pcb", parse_pcblib
                else:
                    continue
                full = os.path.join(dirpath, fn)
                idx.files += 1
                try:
                    if os.path.getsize(full) > MAX_FILE_SIZE:
                        raise ValueError("file too large")
                    entries = parser(full)
                except Exception as e:  # noqa: BLE001 - one bad file never stops the scan
                    log.warning("skipping %s: %s", _rel(full, root), e)
                    idx.failed.append(_rel(full, root))
                    continue
                rel = _rel(full, root)
                getattr(idx, kind).extend(LibEntry(n, rel, root, d) for n, d in entries)
    idx.scanned_at = time.time()
    idx.seconds = time.monotonic() - t0
    return idx.finish()


class LibScanner:
    """Holds the current index; rescans in a background thread."""

    def __init__(self, dirs: tuple[str, ...]):
        self.dirs = dirs
        self.index = LibIndex().finish()
        self.scanning = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.dirs)

    def rescan(self, background: bool = True) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self.scanning:
                return False
            self.scanning = True

        def run():
            try:
                new = scan(self.dirs)
                self.index = new
                log.info("library scan: %d symbols, %d footprints in %d files (%d failed) in %.1fs",
                         len(new.sch), len(new.pcb), new.files, len(new.failed), new.seconds)
            except Exception:  # noqa: BLE001
                log.exception("library scan failed")
            finally:
                self.scanning = False

        if background:
            threading.Thread(target=run, name="libscan", daemon=True).start()
        else:
            run()
        return True
