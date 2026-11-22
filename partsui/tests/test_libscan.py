"""SchLib / PcbLib parsing on real files from the Celestial library (tests/fixtures/libs)."""
import shutil

import olefile

from app import libscan
from conftest import FIXTURE_LIBS

CEL = FIXTURE_LIBS / "celestial"
DAN = FIXTURE_LIBS / "daniel"
CAP_SCH = CEL / "symbols" / "Passives" / "SCH - PASSIVES - CAPACITOR.SCHLIB"
ADS_SCH = CEL / "symbols" / "ADC" / "SCH - ADC - TI ADS1115.SchLib"
CAP_PCB = CEL / "footprints" / "Capacitor - MLCC" / "PCB - CAPACITOR - MLCC - CAP 0402_1005.PCBLIB"
DFN_PCB = CEL / "footprints" / "Leadless - DFN" / "PCB - LEADLESS - DFN - NEXPERIA DFN2020M-6 SOT1220-2 P-CH.PcbLib"


def test_schlib_upper_and_mixed_case_headers():
    assert libscan.parse_schlib(str(CAP_SCH)) == [("Capacitor", "SMT Capacitor")]   # LIBREF0=
    assert libscan.parse_schlib(str(ADS_SCH)) == [("TI ADS1115", "")]                # LibRef0=


def test_pcblib_names_and_descriptions():
    (name, descr), = libscan.parse_pcblib(str(CAP_PCB))
    assert name == "CAP 0402_1005"
    assert descr.startswith("Chip Capacitor")


def test_pcblib_long_name_beyond_ole_storage_limit():
    with olefile.OleFileIO(str(DFN_PCB)) as ole:
        storages = {e[0] for e in ole.listdir(streams=False, storages=True)}
    assert "NEXPERIA DFN2020M-6 SOT1220-2 P" in storages            # truncated to 31 chars
    names = [n for n, _ in libscan.parse_pcblib(str(DFN_PCB))]
    assert names == ["NEXPERIA DFN2020M-6 SOT1220-2 P-CH"]           # full name recovered


def test_library_data_name_list():
    with olefile.OleFileIO(str(DFN_PCB)) as ole:
        assert libscan._library_data_names(ole) == ["NEXPERIA DFN2020M-6 SOT1220-2 P-CH"]


def test_scan_paths_are_relative_to_their_root_with_forward_slashes(tmp_path):
    # Copy so we can add a .git dir (skipped) without touching the fixtures.
    root = tmp_path / "celestial"
    shutil.copytree(CEL, root)
    (root / ".git").mkdir()
    shutil.copy(CAP_SCH, root / ".git" / "hidden.SchLib")
    idx = libscan.scan([str(root), str(DAN), str(tmp_path / "missing")])

    sch = {e.name: e.path for e in idx.sch}
    pcb = {e.name: e.path for e in idx.pcb}
    assert sch == {
        "Capacitor": "symbols/Passives/SCH - PASSIVES - CAPACITOR.SCHLIB",
        "TI ADS1115": "symbols/ADC/SCH - ADC - TI ADS1115.SchLib",
        "Inductor": "Daniel.SchLib",
    }
    assert pcb["CAP 0402_1005"] == "footprints/Capacitor - MLCC/PCB - CAPACITOR - MLCC - CAP 0402_1005.PCBLIB"
    assert "NEXPERIA DFN2020M-6 SOT1220-2 P-CH" in pcb
    assert idx.failed == ["Broken.PcbLib"]                            # logged and skipped, not fatal
    assert idx.missing_roots == [str(tmp_path / "missing")]
    assert idx.files == 6


def test_suggest_prefix_first_and_limited():
    idx = libscan.LibIndex(pcb=[libscan.LibEntry(n, f"{n}.PcbLib", "/r") for n in
                                ["XCAP", "CAP 0402", "CAP 0603", "MYCAP", "RES 0402"]]
                           + [libscan.LibEntry(f"CAP {i:05}", "x.PcbLib", "/r") for i in range(40)]).finish()
    got = [e.name for e in idx.suggest("pcb", "cap", limit=20)]
    assert len(got) == 20
    assert all(n.startswith("CAP") for n in got)                      # prefix matches fill the list first
    few = [e.name for e in idx.suggest("pcb", "0402")]
    assert few == ["CAP 0402", "RES 0402"]                            # contains-matches when no prefix match
    mixed = [e.name for e in idx.suggest("pcb", "cap 06")]
    assert mixed == ["CAP 0603"]


def test_scanner_without_dirs_is_disabled():
    s = libscan.LibScanner(())
    assert not s.enabled and s.rescan() is False and s.index.count == 0
