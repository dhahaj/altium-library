"""End-to-end tests against a real SQL Server 2022 container (see conftest.py).

01_init.sql and 02_partsui.sql run for real, so the logins and permissions are the real ones;
the app connects as `librarian`."""
import re
import time
from html import unescape
from html.parser import HTMLParser

import pyodbc
import pytest

from conftest import DB, VARS, refresh
from seed import LONG_FP_PATH

pytestmark = pytest.mark.integration

CER = "Capacitors - Ceramic - 0603"
RES = "Resistors - Surface Mount - 0402"
MEM = "Memory"
MINE_CER = "Custom - " + CER


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values, self._textarea = {}, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name") and a.get("type") != "checkbox":
            self.values[a["name"]] = a.get("value") or ""
        elif tag == "textarea" and a.get("name"):
            self._textarea = a["name"]
            self.values[a["name"]] = ""

    def handle_data(self, data):
        if self._textarea:
            self.values[self._textarea] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._textarea = None


def form_values(html: str) -> dict:
    """Every input/textarea in the page: name -> value."""
    p = _FormParser()
    p.feed(html)
    return p.values


def search_pns(client, **params):
    r = client.get("/search", params=params, headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    return [unescape(m) for m in re.findall(r'<td class="pn"><a [^>]*>([^<]+)</a>', r.text)]


def index_rows(sa, pn):
    cur = sa.cursor()
    cur.execute("SELECT table_name, is_custom FROM dbo._parts_index WHERE part_number = ?", pn)
    return sorted((t, bool(c)) for t, c in cur.fetchall())


def table_exists(sa, name):
    cur = sa.cursor()
    cur.execute("SELECT OBJECT_ID(?, 'U')", f"dbo.[{name}]")
    return cur.fetchone()[0] is not None


def clone(client, table, pn, new_pn, **changes):
    r = client.get("/parts/clone", params={"table": table, "pn": pn})
    assert r.status_code == 200, r.text
    data = form_values(r.text)
    data["col:Part Number"] = new_pn
    for k, v in changes.items():
        data[k if k == "allow_dup" else "col:" + k] = v
    data["csrf_token"] = client.csrf
    return client.post("/parts/save", data=data)


# ---------------------------------------------------------------- search

def test_search_ranks_part_number_prefix_first(client):
    pns = search_pns(client, q="100n")
    # Part numbers starting with 100N first, then the contains-matches (comment 100nF).
    assert pns[:2] == ["100NF-IN-MEMORY?", "100NF-LOOKALIKE"]
    assert set(pns[2:]) == {"C0603C104K5RAC", "GRM188R71H104KA93D"}


def test_search_matches_comment_description_and_all_terms(client):
    assert search_pns(client, q="kemet") == ["C0603C104K5RAC"]               # description
    assert search_pns(client, q="10uF") == ["CL10A106KP8NNNC"]                # comment
    assert search_pns(client, q="10k panasonic") == ["ERJ-2RKF1002X"]         # every term must match
    assert "RAW-ONLY-0001" not in search_pns(client, q="RAW-ONLY")            # z_ tables are ignored


def test_search_filters(client):
    assert search_pns(client, q="10", table=RES) == ["100NF-LOOKALIKE", "ERJ-2RKF1002X", "RC0402FR-0710KL"]
    assert search_pns(client, q="100n", mine="1") == []
    assert clone(client, CER, "GRM188R71H104KA93D", "DH-C-0001").status_code == 303
    assert search_pns(client, q="100n", mine="1") == ["DH-C-0001"]
    r = client.get("/search", params={"q": "DH-C"}, headers={"HX-Request": "true"})
    assert '<span class="badge">Mine</span>' in r.text
    assert search_pns(client, mine="1") == ["DH-C-0001"]                      # browse without a query
    r = client.get("/search", params={"q": "x"}, headers={"HX-Request": "true"})
    assert "at least 2 characters" in r.text


def test_search_pagination(client, sa):
    cur = sa.cursor()
    cur.execute("EXEC dbo.make_custom_table @name = N'Bulk'")
    cur.fetchall()
    for i in range(120):
        cur.execute("INSERT INTO dbo.[Custom - Bulk] ([Part Number], [Comment], [Library Ref], [Library Path], "
                    "[Footprint Ref], [Footprint Path]) VALUES (?, 'bulk', 'R', 'R.SchLib', 'F', 'F.PcbLib')",
                    f"BULK-{i:04}")
    sa.commit()
    refresh(sa)
    p1, p2, p3 = (search_pns(client, q="BULK", page=str(p)) for p in (1, 2, 3))
    assert len(p1) == 50 and len(p2) == 50 and len(p3) == 20
    assert p1[0] == "BULK-0000" and p2[0] == "BULK-0050" and p3[-1] == "BULK-0119"
    r = client.get("/search", params={"q": "BULK", "page": "2"}, headers={"HX-Request": "true"})
    assert "Next →" in r.text and "← Previous" in r.text


def test_full_page_search_and_categories(client):
    r = client.get("/", params={"q": "GRM188"})
    assert r.status_code == 200
    assert "GRM188R71H104KA93D" in r.text
    assert '<optgroup label="Celestial">' in r.text and f'<option value="{CER}"' in r.text
    assert "z_CapacitorsCeramic" not in r.text and "Custom - Template" not in r.text


def test_search_with_sql_injection_shapes(client, sa):
    for q in ["'; DROP TABLE dbo.[Memory]; --", "%", "_", "[a-z]", "100' OR '1'='1", "\\"]:
        assert client.get("/search", params={"q": q}).status_code == 200
    assert search_pns(client, q="%%") == []                                   # wildcards are literal
    assert search_pns(client, q="1__N") == []
    assert search_pns(client, q="RY?") == ["100NF-IN-MEMORY?"]
    r = client.get("/search", params={"q": "100", "table": "Memory]; DROP TABLE dbo.[Memory]; --"})
    assert r.status_code == 404
    assert table_exists(sa, MEM)


# ---------------------------------------------------------------- detail

def test_part_detail(client):
    r = client.get("/part", params={"table": CER, "pn": "GRM188R71H104KA93D"})
    assert r.status_code == 200
    assert '<a href="https://www.murata.com/en-us/products/productdetail?partno=GRM188R71H104KA93D"' in r.text
    assert "Clone to my library" in r.text and ">Delete<" not in r.text and ">Edit<" not in r.text
    assert 'class="empty"' in r.text and "Show empty fields" in r.text
    r = client.get("/part", params={"table": CER, "pn": "CL10A106KP8NNNC"})
    assert "javascript:alert(1)" in r.text and 'href="javascript:' not in r.text   # not linkified
    assert client.get("/part", params={"table": CER, "pn": "nope"}).status_code == 404
    assert client.get("/part", params={"table": "z_Memory", "pn": "RAW-ONLY-0002"}).status_code == 404


# ---------------------------------------------------------------- clone

def test_clone_into_new_custom_table_creates_it(client, sa):
    assert not table_exists(sa, MINE_CER)
    r = client.get("/parts/clone", params={"table": CER, "pn": "GRM188R71H104KA93D"})
    assert "doesn't exist yet" in r.text
    data = form_values(r.text)
    assert data["col:Part Number"] == "" and 'placeholder="was GRM188R71H104KA93D"' in r.text
    assert data["col:Footprint Path"] == LONG_FP_PATH                        # >200 chars, untruncated
    assert not table_exists(sa, MINE_CER)                                     # nothing created on GET

    r = clone(client, CER, "GRM188R71H104KA93D", "DH-C-0001", Comment="100nF 50V")
    assert r.status_code == 303
    assert "notice=created_table" in r.headers["location"]
    assert table_exists(sa, MINE_CER)
    cur = sa.cursor()
    cur.execute(f"SELECT [Comment], [Footprint Path], [Capacitance] FROM dbo.[{MINE_CER}] WHERE [Part Number] = 'DH-C-0001'")
    assert tuple(cur.fetchone()) == ("100nF 50V", LONG_FP_PATH, "100nF")
    assert index_rows(sa, "DH-C-0001") == [(MINE_CER, True)]

    page = client.get(r.headers["location"])
    assert "New table created" in page.text and "/dblib" in page.text
    assert ">Edit<" in page.text and "Delete" in page.text

    # The second clone reuses the table; cloning a custom part stays in the same table.
    assert clone(client, MINE_CER, "DH-C-0001", "DH-C-0002").status_code == 303
    assert index_rows(sa, "DH-C-0002") == [(MINE_CER, True)]


def test_duplicate_part_number_block_and_override(client, sa):
    r = clone(client, CER, "GRM188R71H104KA93D", "GRM188R71H104KA93D")
    assert r.status_code == 409
    assert "already a Celestial part" in unescape(r.text)
    assert not table_exists(sa, MINE_CER)                                     # refused before creating
    r = clone(client, CER, "GRM188R71H104KA93D", "erj-2rkf1002x")              # any table, any case
    assert r.status_code == 409 and RES in unescape(r.text)

    r = clone(client, CER, "GRM188R71H104KA93D", "GRM188R71H104KA93D", allow_dup="1")
    assert r.status_code == 303
    assert index_rows(sa, "GRM188R71H104KA93D") == [(CER, False), (MINE_CER, True)]

    # A duplicate of one of *my* parts is refused even with the override.
    r = clone(client, MINE_CER, "GRM188R71H104KA93D", "GRM188R71H104KA93D", allow_dup="1")
    assert r.status_code == 409 and "already exists in" in unescape(r.text)


def test_validation_errors_rerender_form(client, sa):
    r = clone(client, MEM, "W25Q128JVSIQ", "DH-M-1", Pins="eight", Released="May 2016", Price="1.23456")
    assert r.status_code == 422
    text = unescape(r.text)
    assert "Pins must be a whole number" in text
    assert "Released must be a date like YYYY-MM-DD" in text
    assert "Price must fit 5 digits before and 4 after" in text
    assert 'value="eight"' in r.text                                          # keeps what was typed
    r = clone(client, MEM, "W25Q128JVSIQ", "DH-M-1", **{"Library Ref": ""})
    assert r.status_code == 422 and "Library Ref is required" in unescape(r.text)
    assert not table_exists(sa, "Custom - Memory")
    r = clone(client, MEM, "W25Q128JVSIQ", "DH-M-1", Pins="16", Released="2020-01-31", Price="0.5")
    assert r.status_code == 303
    cur = sa.cursor()
    cur.execute("SELECT [Pins], [Released], [Price] FROM dbo.[Custom - Memory]")
    row = cur.fetchone()
    assert (row[0], str(row[1]), str(row[2])) == (16, "2020-01-31", "0.5000")


def test_length_and_codepage_validation_on_template_table(client, sa):
    r = client.get("/parts/new", params={"name": "Connectors - Detex"})
    assert r.status_code == 200
    data = {k: v for k, v in form_values(r.text).items()}
    data.update({"col:Part Number": "DTX-J-0001", "col:Comment": "x" * 256, "col:Library Ref": "CONN_2X4",
                 "col:Library Path": "Daniel.SchLib", "col:Footprint Ref": "MOLEX_43045-0800",
                 "col:Footprint Path": "Daniel.PcbLib", "col:Description": "10kΩ pull-up",
                 "csrf_token": client.csrf})
    r = client.post("/parts/save", data=data)
    assert r.status_code == 422
    text = unescape(r.text)
    assert "Comment is 256 characters; the column holds 255" in text
    assert "contains “Ω”" in text
    data.update({"col:Comment": "Molex 2x4", "col:Description": "Micro-Fit 3.0 2x4 RA header"})
    r = client.post("/parts/save", data=data)
    assert r.status_code == 303 and "created_table" in r.headers["location"]
    assert table_exists(sa, "Custom - Connectors - Detex")


# ---------------------------------------------------------------- new blank part

def test_new_part_targets_and_name_clashes(client, sa):
    r = client.get("/parts/new")
    assert "Copy a Celestial category" in r.text
    assert client.get("/parts/new", params={"like": CER}).status_code == 200
    assert not table_exists(sa, MINE_CER)
    for bad in [CER, "custom - " + CER, "Template", "x]; DROP TABLE t", "", "a.b"]:
        r = client.get("/parts/new", params={"name": bad})
        assert r.status_code == 400, bad
    assert clone(client, CER, "GRM188R71H104KA93D", "DH-C-0009").status_code == 303
    r = client.get("/parts/new", params={"name": CER})               # now Custom - <it> exists too
    assert r.status_code == 400 and "already exists" in unescape(r.text)
    r = client.get("/parts/new", params={"table": MINE_CER})
    assert r.status_code == 200 and form_values(r.text)["col:Part Number"] == ""
    assert client.get("/parts/new", params={"table": CER}).status_code == 403


# ---------------------------------------------------------------- edit + delete

def test_edit_with_key_change(client, sa):
    assert clone(client, CER, "GRM188R71H104KA93D", "DH-C-0100").status_code == 303
    r = client.get("/parts/edit", params={"table": MINE_CER, "pn": "DH-C-0100"})
    data = form_values(r.text)
    assert data["original_pn"] == "DH-C-0100" and data["col:Part Number"] == "DH-C-0100"
    data.update({"col:Part Number": "DH-C-0101", "col:Comment": "edited", "csrf_token": client.csrf})
    r = client.post("/parts/save", data=data)
    assert r.status_code == 303 and "pn=DH-C-0101" in r.headers["location"]
    cur = sa.cursor()
    cur.execute(f"SELECT [Part Number], [Comment] FROM dbo.[{MINE_CER}]")
    assert [tuple(x) for x in cur.fetchall()] == [("DH-C-0101", "edited")]
    assert index_rows(sa, "DH-C-0100") == []
    assert index_rows(sa, "DH-C-0101") == [(MINE_CER, True)]

    # Key change onto an existing Celestial number needs the override; onto my own is refused.
    data.update({"original_pn": "DH-C-0101", "col:Part Number": "RC0402FR-0710KL"})
    assert client.post("/parts/save", data=data).status_code == 409
    assert clone(client, MINE_CER, "DH-C-0101", "DH-C-0200").status_code == 303
    data.update({"col:Part Number": "DH-C-0200", "allow_dup": "1"})
    assert client.post("/parts/save", data=data).status_code == 409

    # Saving without changing the key is fine (even if it duplicates Celestial).
    data.update({"col:Part Number": "DH-C-0101", "col:Comment": "again"})
    data.pop("allow_dup")
    assert client.post("/parts/save", data=data).status_code == 303


def test_delete(client, sa):
    assert clone(client, CER, "GRM188R71H104KA93D", "DH-C-0300").status_code == 303
    r = client.post("/parts/delete", data={"table": MINE_CER, "pn": "DH-C-0300", "csrf_token": client.csrf})
    assert r.status_code == 303 and "notice=deleted" in r.headers["location"]
    cur = sa.cursor()
    cur.execute(f"SELECT COUNT(*) FROM dbo.[{MINE_CER}]")
    assert cur.fetchone()[0] == 0
    assert index_rows(sa, "DH-C-0300") == []
    r = client.post("/parts/delete", data={"table": MINE_CER, "pn": "DH-C-0300", "csrf_token": client.csrf})
    assert r.status_code == 404


# ---------------------------------------------------------------- the write guard, end to end

def test_writes_to_celestial_tables_are_forbidden_even_with_forged_forms(client, sa):
    before = index_rows(sa, "GRM188R71H104KA93D")
    forged = [
        {"mode": "edit", "target": CER, "create": "", "original_pn": "GRM188R71H104KA93D",
         "col:Part Number": "GRM188R71H104KA93D", "col:Comment": "HACKED"},
        {"mode": "new", "target": CER, "create": "", "col:Part Number": "EVIL-1", "col:Comment": "x",
         "col:Description": "x", "col:Library Ref": "x", "col:Library Path": "x", "col:Footprint Ref": "x",
         "col:Footprint Path": "x"},
        {"mode": "new", "target": "Custom - Template", "create": "", "col:Part Number": "EVIL-2"},
        {"mode": "new", "target": "z_Memory", "create": "", "col:Part Number": "EVIL-3"},
        {"mode": "new", "target": "_parts_index", "create": "", "col:Part Number": "EVIL-4"},
        {"mode": "new", "target": f"{MINE_CER}]; DROP TABLE dbo.[{CER}]; --", "create": "",
         "col:Part Number": "EVIL-5"},
    ]
    for data in forged:
        r = client.post("/parts/save", data={**data, "csrf_token": client.csrf})
        assert r.status_code == 403, data
    for table in (CER, "Custom - Template", "_mirror_log"):
        r = client.post("/parts/delete", data={"table": table, "pn": "GRM188R71H104KA93D", "csrf_token": client.csrf})
        assert r.status_code == 403
    assert client.get("/parts/edit", params={"table": CER, "pn": "GRM188R71H104KA93D"}).status_code == 403
    # "like" must name a Celestial category, and edit can't create tables.
    r = client.post("/parts/save", data={"mode": "edit", "create": "like", "create_arg": CER,
                                         "csrf_token": client.csrf})
    assert r.status_code == 400
    cur = sa.cursor()
    cur.execute(f"SELECT [Comment] FROM dbo.[{CER}] WHERE [Part Number] = 'GRM188R71H104KA93D'")
    assert cur.fetchone()[0] == "100nF"
    cur.execute(f"SELECT COUNT(*) FROM dbo.[{CER}]")
    assert cur.fetchone()[0] == 3
    assert table_exists(sa, CER)
    assert index_rows(sa, "GRM188R71H104KA93D") == before


def test_injection_shaped_column_names_are_rejected(client, sa):
    r = client.get("/parts/clone", params={"table": CER, "pn": "GRM188R71H104KA93D"})
    data = form_values(r.text)
    data.update({"col:Part Number": "DH-X-1", "csrf_token": client.csrf,
                 f"col:Comment] = 'x'; DROP TABLE dbo.[{CER}]; --": "boom"})
    assert client.post("/parts/save", data=data).status_code == 400
    data.pop(f"col:Comment] = 'x'; DROP TABLE dbo.[{CER}]; --")
    data["col:comment"] = "wrong case"                                         # exact match only
    assert client.post("/parts/save", data=data).status_code == 400
    assert table_exists(sa, CER) and not table_exists(sa, MINE_CER)
    # Injection-shaped values are just values.
    data.pop("col:comment")
    data["col:Comment"] = "'; DROP TABLE dbo.[Memory]; --"
    assert client.post("/parts/save", data=data).status_code == 303
    assert table_exists(sa, MEM)
    cur = sa.cursor()
    cur.execute(f"SELECT [Comment] FROM dbo.[{MINE_CER}] WHERE [Part Number] = 'DH-X-1'")
    assert cur.fetchone()[0] == "'; DROP TABLE dbo.[Memory]; --"


def test_csrf_required_on_real_forms(client, sa):
    data = form_values(client.get("/parts/clone", params={"table": CER, "pn": "GRM188R71H104KA93D"}).text)
    data["col:Part Number"] = "DH-CSRF"
    data.pop("csrf_token")
    assert client.post("/parts/save", data=data).status_code == 403
    assert client.post("/parts/save", data={**data, "csrf_token": "x" * 43}).status_code == 403
    assert not table_exists(sa, MINE_CER)


# ---------------------------------------------------------------- permissions

def test_reader_login_cannot_see_parts_index(mssql):
    server, _ = mssql
    reader = pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server};DATABASE={DB};UID={VARS['READER_USER']};"
        f"PWD={{{VARS['READER_PASSWORD']}}};Encrypt=yes;TrustServerCertificate=yes")
    cur = reader.cursor()
    with pytest.raises(pyodbc.Error) as e:
        cur.execute("SELECT TOP (1) * FROM dbo._parts_index")
    assert "229" in str(e.value) or "permission" in str(e.value).lower()
    cur.execute("SELECT name FROM sys.tables")
    visible = {r[0] for r in cur.fetchall()}
    assert "_parts_index" not in visible and "_mirror_log" not in visible and "Custom - Template" not in visible
    assert CER in visible                                                      # Altium still sees parts
    with pytest.raises(pyodbc.Error):
        cur.execute("EXEC dbo.refresh_parts_index")
    reader.close()


def test_librarian_has_only_the_new_proc_grants(sa):
    cur = sa.cursor()
    cur.execute("""SELECT OBJECT_NAME(major_id), permission_name, state_desc FROM sys.database_permissions
                   WHERE grantee_principal_id = USER_ID(?) AND class = 1 ORDER BY 1""", VARS["LIBRARIAN_USER"])
    assert [tuple(r) for r in cur.fetchall()] == [
        ("make_custom_table", "EXECUTE", "GRANT"),
        ("refresh_parts_index", "EXECUTE", "GRANT"),
        ("upsert_parts_index_row", "EXECUTE", "GRANT")]


def test_upsert_proc_rejects_non_part_tables(sa):
    cur = sa.cursor()
    for t in ("z_Memory", "_mirror_log", "Custom - Template", "nope", None):
        with pytest.raises(pyodbc.Error):
            cur.execute("EXEC dbo.upsert_parts_index_row @table = ?, @part_number = 'x'", t)
        sa.rollback()


def test_scripts_are_idempotent(mssql, sa):
    from sqlrunner import run_file
    from conftest import REPO, _sa_connect
    server, pw = mssql
    master = _sa_connect(server, pw)
    run_file(master, REPO / "sql" / "01_init.sql", VARS)
    master.cursor().execute(f"USE [{DB}]")
    run_file(master, REPO / "sql" / "02_partsui.sql", VARS)
    master.close()
    cur = sa.cursor()
    cur.execute("SELECT COUNT(*) FROM dbo._parts_index")
    assert cur.fetchone()[0] == 8


# ---------------------------------------------------------------- DbLib page

def test_dblib_check_and_patch_round_trip(client, sa):
    from test_dblib import make_dblib
    assert clone(client, CER, "GRM188R71H104KA93D", "DH-C-0400").status_code == 303
    original = make_dblib(spaced=True, nl="\r\n", blank=True, tables=[(CER, True), (MEM, True)])

    r = client.post("/dblib/check", files={"dblib": ("Daniel.DbLib", original)},
                    data={"csrf_token": client.csrf}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert MINE_CER in r.text and "Missing" in r.text and "will be added" in r.text

    r = client.post("/dblib/patch", files={"dblib": ("Daniel.DbLib", original)}, data={"csrf_token": client.csrf})
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="Daniel.DbLib"'
    assert r.headers["x-tables-added"] == "1"
    out = r.content
    assert out[:len(original)] == original
    tail = out[len(original):].decode("latin-1")
    assert f"[Table3]\r\nSchemaName=\r\nTableName={MINE_CER}\r\nEnabled=True\r\n" in tail
    assert f"Options=FieldName={MINE_CER}.Part Number|" in tail
    assert f"FieldNameOnly=Capacitance" not in tail

    r = client.post("/dblib/check", files={"dblib": ("Daniel.DbLib", out)}, data={"csrf_token": client.csrf})
    assert "Enabled</span> (Table3)" in r.text and "Nothing to add" in r.text
    r = client.post("/dblib/patch", files={"dblib": ("x.txt", b"hello")}, data={"csrf_token": client.csrf})
    assert r.status_code == 400 and "doesn't look like a .DbLib" in unescape(r.text)
    r = client.post("/dblib/patch", files={"dblib": ("../../etc/passwd.DbLib", original)},
                    data={"csrf_token": client.csrf})
    assert r.headers["content-disposition"] == 'attachment; filename="passwd.DbLib"'


def test_dblib_upload_size_limit(client):
    big = b"[DatabaseLinks]\r\n" + b"x" * (9 * 1024 * 1024)
    r = client.post("/dblib/patch", files={"dblib": ("Daniel.DbLib", big)}, data={"csrf_token": client.csrf})
    assert r.status_code == 400 and "too big" in r.text


# ---------------------------------------------------------------- libraries, status, reindex

def test_library_autocomplete(client):
    r = client.get("/parts/clone", params={"table": CER, "pn": "GRM188R71H104KA93D"})
    assert 'hx-get="/libs/suggest"' in r.text and 'data-path-field="f-footprint-path-' in r.text
    m = re.search(r'id="(f-footprint-ref-[a-z0-9]+)"', r.text)
    r = client.get("/libs/suggest", params={"kind": "pcb", "field": m.group(1), "col": "Footprint Ref",
                                            "col:Footprint Ref": "nexp"})
    assert 'data-ref="NEXPERIA DFN2020M-6 SOT1220-2 P-CH"' in r.text
    assert 'data-path="footprints/Leadless - DFN/PCB - LEADLESS - DFN - NEXPERIA DFN2020M-6 SOT1220-2 P-CH.PcbLib"' in r.text
    r = client.get("/libs/suggest", params={"kind": "sch", "field": "f-x", "q": "ind"})
    assert 'data-ref="Inductor" data-path="Daniel.SchLib"' in r.text
    assert client.get("/libs/suggest", params={"kind": "evil", "field": "f-x", "q": "a"}).status_code == 400
    assert client.get("/libs/suggest", params={"kind": "sch", "field": '"><script>', "q": "a"}).status_code == 400


def test_status_and_reindex(client, sa):
    r = client.get("/status")
    assert "Connected" in r.text and "Celestial parts" in r.text
    assert re.search(r"<dt>Celestial parts</dt><dd>8</dd>", r.text)
    assert "5 symbols" not in r.text and "3 symbols, 2 footprints" in r.text and "1 unreadable" in r.text
    cur = sa.cursor()
    cur.execute("INSERT INTO dbo._mirror_log (table_name, row_count, status) VALUES ('Memory', 2, 'ok'), ('Bad', 0, 'failed')")
    cur.execute("DELETE FROM dbo._parts_index WHERE table_name = 'Memory'")      # make it stale
    sa.commit()
    r = client.post("/reindex", data={"csrf_token": client.csrf})
    assert r.status_code == 303 and "notice=reindexed" in r.headers["location"] and "n=8" in r.headers["location"]
    r = client.get(r.headers["location"])
    assert "Search index rebuilt: 8 parts" in r.text
    assert "1 tables copied" in r.text and "1 failed" in r.text
    assert index_rows(sa, "W25Q128JVSIQ") == [(MEM, False)]
    mini = client.get("/status/mini")
    assert "8 Celestial" in mini.text and "0 mine" in mini.text


def test_mirror_hook_refreshes_index(sa, mssql, monkeypatch):
    import importlib.util
    from conftest import REPO
    spec = importlib.util.spec_from_file_location("mirror", REPO / "mirror" / "mirror.py")
    mirror = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mirror)
    cur = sa.cursor()
    cur.execute("DELETE FROM dbo._parts_index")
    sa.commit()
    mirror.refresh_parts_index(sa)
    cur.execute("SELECT COUNT(*) FROM dbo._parts_index")
    assert cur.fetchone()[0] == 8
    # Missing proc: skipped quietly.
    cur.execute("EXEC sp_rename 'dbo.refresh_parts_index', 'refresh_parts_index_x'")
    sa.commit()
    try:
        mirror.refresh_parts_index(sa)
    finally:
        cur.execute("EXEC sp_rename 'dbo.refresh_parts_index_x', 'refresh_parts_index'")
        sa.commit()


# ---------------------------------------------------------------- performance

@pytest.mark.slow
def test_refresh_parts_index_speed_at_full_size(sa):
    """~260k rows over 170 tables, like the real mirror. Must be well under a minute."""
    cur = sa.cursor()
    names = [f"Perf - Category {i:03}" for i in range(170)]
    try:
        for i, name in enumerate(names):
            extra = ", [Comment] varchar(max)" if i % 3 else ""                 # some tables lack Comment
            cur.execute(f"CREATE TABLE dbo.[{name}] ([Part Number] varchar(900) NOT NULL, [Description] varchar(max), "
                        f"[Library Ref] varchar(max), [Footprint Ref] varchar(max){extra})")
            cols = "[Part Number], [Description], [Library Ref], [Footprint Ref]" + (", [Comment]" if i % 3 else "")
            vals = ("CONCAT('PERF-', {i}, '-', n), CONCAT('A fairly typical description for part ', n, "
                    "' with some words in it 0.1uF 50V X7R'), 'Capacitor', 'CAP 0603_1608'"
                    + (", '100nF'" if i % 3 else "")).format(i=i)
            cur.execute(f"""WITH n AS (SELECT TOP (1530) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n
                                       FROM sys.all_objects a CROSS JOIN sys.all_objects b)
                            INSERT INTO dbo.[{name}] ({cols}) SELECT {vals} FROM n""")
        sa.commit()
        t0 = time.monotonic()
        total, custom = refresh(sa)
        elapsed = time.monotonic() - t0
        print(f"\nrefresh_parts_index: {total} rows in {elapsed:.1f}s")
        assert total >= 260_000
        assert elapsed < 30
        from app import parts
        for q in ("zzqq", "x7r", "typical 0.1uF"):
            t0 = time.monotonic()
            result = parts.search(sa, q)
            elapsed = time.monotonic() - t0
            print(f"search {q!r}: {len(result.rows)} rows in {elapsed:.2f}s")
            assert elapsed < 2
    finally:
        for name in names:
            cur.execute(f"IF OBJECT_ID(N'dbo.[{name}]') IS NOT NULL DROP TABLE dbo.[{name}]")
        sa.commit()
        refresh(sa)
