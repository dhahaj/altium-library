"""Read an Altium .DbLib and append table + field-map sections for new custom tables.

The file is INI-style latin-1 text. We never rewrite it: the output is the input bytes
unchanged, followed by the new sections. Line endings follow the input (CRLF in files Altium
saves). Both `key=value` (Altium) and `key = value` (some edited copies) are read; new
sections are written as `key=value`.

The field-map rules copy what Celestial's own DbLib does for its 170 category tables:
only the system columns get a [FieldMapN]; ordinary parameter columns (Capacitance,
Manufacturer 1, ...) get none, and Altium's default maps each to a parameter of the same
name. Set EXPLICIT_PARAMETER_MAPS = True to write a map for every column instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EXPLICIT_PARAMETER_MAPS = False

SYSTEM_FIELDS = (
    "Library Ref", "Library Path",
    "Footprint Ref", "Footprint Path",
    "Footprint Ref 2", "Footprint Path 2",
    "Footprint Ref 3", "Footprint Path 3",
)
USER_WHERE_TEXT = "[Part Number] = '{Part Number}'"

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_NUMBERED = re.compile(r"^(Table|FieldMap)(\d+)$", re.IGNORECASE)


class DbLibError(ValueError):
    pass


@dataclass
class TableEntry:
    number: int
    name: str
    enabled: bool


@dataclass
class DbLibInfo:
    newline: str
    blank_between_sections: bool
    tables: list[TableEntry] = field(default_factory=list)
    max_table: int = 0
    max_fieldmap: int = 0
    has_header: bool = False

    def find(self, table_name: str) -> TableEntry | None:
        low = table_name.casefold()
        return next((t for t in self.tables if t.name.casefold() == low), None)


def _split_kv(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    k, v = line.split("=", 1)
    return k.strip(), v.strip()


def parse(data: bytes) -> DbLibInfo:
    if len(data) == 0:
        raise DbLibError("The file is empty.")
    text = data.decode("latin-1")
    if "\x00" in text[:4096]:
        raise DbLibError("This doesn't look like a .DbLib file (it's binary).")
    newline = "\r\n" if "\r\n" in text else ("\r" if "\r" in text and "\n" not in text else "\n")
    lines = text.split(newline)
    info = DbLibInfo(newline=newline, blank_between_sections=False)

    current: dict | None = None
    seen_sections = 0
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m:
            seen_sections += 1
            if seen_sections == 2 and i > 0 and lines[i - 1].strip() == "":
                info.blank_between_sections = True
            name = m.group(1).strip()
            if name.lower() in ("databaselinks", "outputdatabaselinkfile"):
                info.has_header = True
            nm = _NUMBERED.match(name)
            current = None
            if nm:
                kind, num = nm.group(1).lower(), int(nm.group(2))
                if kind == "table":
                    info.max_table = max(info.max_table, num)
                    current = {"number": num, "name": None, "enabled": False}
                    info.tables.append(current)  # type: ignore[arg-type]
                else:
                    info.max_fieldmap = max(info.max_fieldmap, num)
            continue
        if current is not None:
            kv = _split_kv(line)
            if kv:
                k = kv[0].lower()
                if k == "tablename":
                    current["name"] = kv[1]
                elif k == "enabled":
                    current["enabled"] = kv[1].lower() == "true"

    if not info.has_header:
        raise DbLibError("This doesn't look like a .DbLib file (no [DatabaseLinks] section).")
    info.tables = [TableEntry(t["number"], t["name"] or "", t["enabled"])  # type: ignore[index]
                   for t in info.tables]
    return info


def field_map_options(table: str, column: str) -> str | None:
    """The Options= value for one column, or None when Celestial wouldn't write a map."""
    if column == "Part Number":
        ftype, param, visible = 0, "Part Number", False
    elif column in SYSTEM_FIELDS or column == "Description":
        ftype, param, visible = 1, f"[{column}]", False
    elif column in ("Comment", "Value"):
        ftype, param, visible = 1, column, True
    elif EXPLICIT_PARAMETER_MAPS:
        ftype, param, visible = 1, column, False
    else:
        return None
    return (f"FieldName={table}.{column}|TableNameOnly={table}|FieldNameOnly={column}"
            f"|FieldType={ftype}|ParameterName={param}|VisibleOnAdd={visible}"
            f"|AddMode=0|RemoveMode=0|UpdateMode=0")


def _check_text(s: str, what: str) -> None:
    try:
        s.encode("latin-1")
    except UnicodeEncodeError:
        raise DbLibError(f"{what} “{s}” has characters a .DbLib can't hold.") from None
    if any(ch in s for ch in "\r\n|"):
        raise DbLibError(f"{what} “{s}” contains a line break or '|'.")


def patch(data: bytes, tables: list[tuple[str, list[str]]]) -> tuple[bytes, list[str]]:
    """Append [TableN] + [FieldMapM] sections for every table in `tables` (name, columns)
    that the file doesn't list yet. Returns (new bytes, names added)."""
    info = parse(data)
    nl = info.newline
    sep = nl if info.blank_between_sections else ""
    added: list[str] = []
    out: list[str] = []
    t_num, f_num = info.max_table, info.max_fieldmap
    fieldmaps: list[str] = []

    for name, columns in tables:
        if info.find(name) is not None or name in added:
            continue
        _check_text(name, "Table name")
        t_num += 1
        out.append(f"[Table{t_num}]{nl}SchemaName={nl}TableName={name}{nl}Enabled=True{nl}"
                   f"UserWhere=0{nl}UserWhereText={USER_WHERE_TEXT}{nl}{sep}")
        for col in columns:
            opts = field_map_options(name, col)
            if opts is None:
                continue
            _check_text(col, "Column name")
            f_num += 1
            fieldmaps.append(f"[FieldMap{f_num}]{nl}Options={opts}{nl}{sep}")
        added.append(name)

    if not added:
        return data, []

    text = data.decode("latin-1")
    lead = ""
    if not text.endswith(nl):
        lead = nl
    if info.blank_between_sections and not text.endswith(nl + nl):
        lead += nl
    appended = lead + "".join(out) + "".join(fieldmaps)
    if sep and appended.endswith(nl + nl):
        appended = appended[: -len(nl)]  # match the original: no blank line at the very end
    return data + appended.encode("latin-1"), added
