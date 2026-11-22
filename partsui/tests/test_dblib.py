"""DbLib parsing and patching (no database needed)."""
import pytest

from app import dblib

PREFIX = "Custom - "


def make_dblib(spaced: bool, nl: str = "\r\n", blank: bool = False, tables=None, fieldmaps=3,
               trailing_newline: bool = True) -> bytes:
    """A small file in the same shape as Daniel.DbLib (spaced, CRLF, blank lines) or as an
    Altium-saved one (key=value, no blank lines)."""
    eq = " = " if spaced else "="
    tables = tables or [("Capacitors - Ceramic - 0603", True), ("z_CapacitorsCeramic", False),
                        ("Resistors - Surface Mount - 0402", True)]
    secs = [
        ["[OutputDatabaseLinkFile]", f"Version{eq}1.1"],
        ["[DatabaseLinks]",
         f"ConnectionString{eq}Provider=MSOLEDBSQL19;Data Source=192.0.2.1,1433;User ID=altium_reader;Password=NOT-A-REAL-PASSWORD;",
         f"AddMode{eq}3", f"LeftQuote{eq}[", f"RightQuote{eq}]", f"LastFocusedTable{eq}Memory"],
    ]
    for i, (name, enabled) in enumerate(tables, 1):
        secs.append([f"[Table{i}]", f"SchemaName{eq}", f"TableName{eq}{name}", f"Enabled{eq}{enabled}",
                     f"UserWhere{eq}0", f"UserWhereText{eq}[Part Number] = '{{Part Number}}'"])
    for i in range(1, fieldmaps + 1):
        secs.append([f"[FieldMap{i}]",
                     f"Options{eq}FieldName=Memory.Part Number|TableNameOnly=Memory|FieldNameOnly=Part Number"
                     f"|FieldType=0|ParameterName=Part Number|VisibleOnAdd=False|AddMode=0|RemoveMode=0|UpdateMode=0"])
    joiner = (nl + nl) if blank else nl
    text = joiner.join(nl.join(s) for s in secs)
    if trailing_newline:
        text += nl
    return text.encode("latin-1")


COLS = ["Part Number", "Comment", "Description", "Value", "Library Ref", "Library Path",
        "Footprint Ref", "Footprint Path", "Footprint Ref 2", "Footprint Path 2",
        "Capacitance", "Manufacturer 1", "ComponentLink1URL"]


@pytest.mark.parametrize("spaced,nl,blank", [(True, "\r\n", True), (False, "\r\n", False),
                                             (False, "\n", False), (True, "\r\n", False)])
def test_parse_both_styles(spaced, nl, blank):
    info = dblib.parse(make_dblib(spaced, nl, blank))
    assert info.newline == nl
    assert info.blank_between_sections is blank
    assert info.max_table == 3 and info.max_fieldmap == 3
    assert [(t.number, t.name, t.enabled) for t in info.tables] == [
        (1, "Capacitors - Ceramic - 0603", True), (2, "z_CapacitorsCeramic", False),
        (3, "Resistors - Surface Mount - 0402", True)]


@pytest.mark.parametrize("spaced,nl,blank", [(True, "\r\n", True), (False, "\r\n", False), (False, "\n", False)])
def test_patch_round_trip_keeps_original_bytes(spaced, nl, blank):
    original = make_dblib(spaced, nl, blank)
    out, added = dblib.patch(original, [(PREFIX + "Capacitors - Ceramic - 0603", COLS),
                                        (PREFIX + "Connectors - Detex", ["Part Number", "Comment"])])
    assert added == [PREFIX + "Capacitors - Ceramic - 0603", PREFIX + "Connectors - Detex"]
    assert out[:len(original)] == original                      # nothing before the append changed
    tail = out[len(original):].decode("latin-1")
    if nl == "\r\n":
        assert "\n" not in tail.replace("\r\n", "")              # CRLF everywhere, no bare LF
    else:
        assert "\r" not in tail
    assert out.endswith(nl.encode()) and not out.endswith((nl + nl).encode())

    info = dblib.parse(out)
    assert [t.number for t in info.tables] == [1, 2, 3, 4, 5]
    assert info.find(PREFIX + "Capacitors - Ceramic - 0603").enabled
    assert info.find(PREFIX + "Connectors - Detex").number == 5

    lines = tail.split(nl)
    assert f"[Table4]" in lines and "TableName=Custom - Capacitors - Ceramic - 0603" in lines
    assert "UserWhereText=[Part Number] = '{Part Number}'" in lines
    assert all(" = " not in l.split("|")[0] for l in lines if l.startswith(("Table", "Enabled", "Options")))
    fieldmaps = [l for l in lines if l.startswith("[FieldMap")]
    assert fieldmaps[0] == "[FieldMap4]"
    assert [int(f[9:-1]) for f in fieldmaps] == list(range(4, 4 + len(fieldmaps)))


def test_patch_blank_line_style_matches_file():
    original = make_dblib(True, "\r\n", blank=True)
    out, _ = dblib.patch(original, [(PREFIX + "Memory", ["Part Number", "Comment"])])
    tail = out[len(original):].decode("latin-1")
    assert tail.startswith("\r\n[Table4]\r\n")                    # blank line before, like the rest
    assert "\r\n\r\n[FieldMap4]\r\n" in tail


def test_patch_adds_newline_if_file_lacks_one():
    original = make_dblib(False, "\r\n", trailing_newline=False)
    out, _ = dblib.patch(original, [(PREFIX + "Memory", ["Part Number"])])
    assert out.startswith(original)
    assert out[len(original):].startswith(b"\r\n[Table4]\r\n")


def test_field_map_rules_follow_celestial():
    out, _ = dblib.patch(make_dblib(False), [(PREFIX + "Caps", COLS)])
    opts = {}
    for line in out.decode("latin-1").split("\r\n"):
        if line.startswith("Options=") and "TableNameOnly=Custom - Caps|" in line:
            kv = dict(p.split("=", 1) for p in line[len("Options="):].split("|"))
            opts[kv["FieldNameOnly"]] = kv
    assert opts["Part Number"]["FieldType"] == "0" and opts["Part Number"]["ParameterName"] == "Part Number"
    for col in ("Library Ref", "Library Path", "Footprint Ref", "Footprint Path", "Footprint Ref 2",
                "Footprint Path 2", "Description"):
        assert opts[col]["FieldType"] == "1" and opts[col]["ParameterName"] == f"[{col}]"
        assert opts[col]["VisibleOnAdd"] == "False"
    assert opts["Comment"]["ParameterName"] == "Comment" and opts["Comment"]["VisibleOnAdd"] == "True"
    assert opts["Value"]["VisibleOnAdd"] == "True"
    # Celestial writes no map for ordinary parameters; Altium's default names them after the column.
    assert "Capacitance" not in opts and "Manufacturer 1" not in opts and "ComponentLink1URL" not in opts
    assert opts["Comment"]["FieldName"] == "Custom - Caps.Comment"
    assert all(o["AddMode"] == o["RemoveMode"] == o["UpdateMode"] == "0" for o in opts.values())


def test_explicit_parameter_maps_switch(monkeypatch):
    monkeypatch.setattr(dblib, "EXPLICIT_PARAMETER_MAPS", True)
    assert "ParameterName=Capacitance|VisibleOnAdd=False" in dblib.field_map_options("T", "Capacitance")


def test_existing_tables_are_not_added_again_case_insensitive():
    original = make_dblib(False, tables=[(PREFIX + "Memory", False), ("Memory", True)])
    out, added = dblib.patch(original, [(PREFIX + "MEMORY", ["Part Number"])])
    assert added == [] and out == original
    info = dblib.parse(original)
    assert info.find(PREFIX + "memory").enabled is False


def test_numbering_continues_after_gaps():
    original = make_dblib(False).replace(b"[Table3]", b"[Table17]").replace(b"[FieldMap3]", b"[FieldMap40]")
    out, _ = dblib.patch(original, [(PREFIX + "X", ["Part Number", "Comment"])])
    tail = out[len(original):].decode()
    assert "[Table18]" in tail and "[FieldMap41]" in tail and "[FieldMap42]" in tail


def test_rejects_non_dblib_and_binary():
    with pytest.raises(dblib.DbLibError):
        dblib.parse(b"")
    with pytest.raises(dblib.DbLibError):
        dblib.parse(b"\xd0\xcf\x11\xe0\x00\x00 not text")
    with pytest.raises(dblib.DbLibError):
        dblib.parse(b"[Something]\r\nKey=Value\r\n")


def test_rejects_names_latin1_cannot_hold():
    with pytest.raises(dblib.DbLibError):
        dblib.patch(make_dblib(False), [(PREFIX + "Résistors Ω", ["Part Number"])])
