"""Write guard, identifier quoting, value validation, config and auth/CSRF (no database)."""
import base64
import datetime as dt
import decimal

import pytest
from fastapi.testclient import TestClient

from app import db, parts
from app.config import ConfigError, Settings, load_settings
from app.db import Catalog, Column, NotWritable, Table, quote_ident, writable_table

PREFIX = "Custom - "


def col(name, dtype="varchar", length=255, nullable=True, **kw):
    return Column(name=name, data_type=dtype, max_length=length, precision=kw.get("precision"),
                  scale=kw.get("scale"), nullable=nullable, has_default=False, is_identity=False,
                  is_computed=False, code_page=kw.get("code_page", 1252 if dtype in ("varchar", "char") else None))


def table(name, custom, view=False):
    return Table(name=name, is_view=view, is_custom=custom, columns=(col("Part Number", nullable=False),))


@pytest.fixture
def catalog():
    tables = {t.name: t for t in [
        table("Capacitors - Ceramic - 0603", False),
        table(PREFIX + "Capacitors - Ceramic - 0603", True),
        table(PREFIX + "A View", False, view=True),
    ]}
    return Catalog(tables, table(PREFIX + "Template", False), PREFIX, ["z_Raw", "_mirror_log"])


# ---------------------------------------------------------------- write guard

def test_guard_allows_only_custom_base_tables(catalog):
    assert writable_table(catalog, PREFIX + "Capacitors - Ceramic - 0603").is_custom
    for name in ["Capacitors - Ceramic - 0603",           # Celestial
                 PREFIX + "Template",                      # the template
                 PREFIX + "A View",                        # a view, even with the prefix
                 PREFIX + "Does Not Exist",                # not in the catalog
                 "custom - capacitors - ceramic - 0603",   # case games
                 " " + PREFIX + "Capacitors - Ceramic - 0603",
                 PREFIX + "Capacitors - Ceramic - 0603]; DROP TABLE x; --",
                 "z_Raw", "_mirror_log", "", None, 42]:
        with pytest.raises(NotWritable):
            writable_table(catalog, name)


def test_guard_is_used_by_every_write_function(catalog):
    class NoConn:
        def cursor(self):
            raise AssertionError("must not reach the database")
    for fn, args in [(parts.insert_part, ({"Part Number": "X"},)),
                     (parts.update_part, ("X", {"Part Number": "X"})),
                     (parts.delete_part, ("X",))]:
        with pytest.raises(NotWritable):
            fn(NoConn(), catalog, "Capacitors - Ceramic - 0603", *args)


def test_quote_ident():
    assert quote_ident("Capacitors - Ceramic - 0603") == "[Capacitors - Ceramic - 0603]"
    assert quote_ident("a]b") == "[a]]b]"
    assert quote_ident("x]; DROP TABLE t; --") == "[x]]; DROP TABLE t; --]"
    for bad in ("", None, "a\x00b"):
        with pytest.raises(ValueError):
            quote_ident(bad)


def test_catalog_exact_match_only(catalog):
    with pytest.raises(db.UnknownTable):
        catalog.get("capacitors - ceramic - 0603")
    assert catalog.exists_ci("CAPACITORS - CERAMIC - 0603")
    assert catalog.exists_ci("z_raw")
    assert catalog.custom_target_for(catalog.get("Capacitors - Ceramic - 0603")) == PREFIX + "Capacitors - Ceramic - 0603"


def test_like_escape():
    assert parts.like_escape("10%_[x]\\") == "10\\%\\_\\[x]\\\\"


# ---------------------------------------------------------------- validation

def test_coerce_text_length_and_codepage():
    c = col("Comment", length=5)
    assert parts.coerce(c, "  10uF ") == "10uF"
    assert parts.coerce(c, "   ") is None
    with pytest.raises(ValueError, match="holds 5"):
        parts.coerce(c, "123456")
    assert parts.coerce(col("Comment"), "4.7µF ±10%") == "4.7µF ±10%"        # fine in cp1252
    with pytest.raises(ValueError, match="Ω"):
        parts.coerce(col("Comment"), "10kΩ")                                 # not in cp1252
    assert parts.coerce(col("Comment", "nvarchar"), "10kΩ") == "10kΩ"
    assert parts.coerce(col("Description", length=-1), "x" * 10000)          # (max)


def test_coerce_numbers_dates():
    assert parts.coerce(col("Pins", "int", None), "8") == 8
    with pytest.raises(ValueError):
        parts.coerce(col("Pins", "int", None), "eight")
    with pytest.raises(ValueError):
        parts.coerce(col("Pins", "tinyint", None), "300")
    d = col("Price", "decimal", None, precision=9, scale=4)
    assert parts.coerce(d, "0.42") == decimal.Decimal("0.42")
    assert parts.coerce(d, "12345.6789") == decimal.Decimal("12345.6789")
    for bad in ("0.00001", "123456", "abc", "NaN"):
        with pytest.raises(ValueError):
            parts.coerce(d, bad)
    assert parts.coerce(col("R", "float", None), "1e3") == 1000.0
    assert parts.coerce(col("D", "date", None), "2016-05-01") == dt.date(2016, 5, 1)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parts.coerce(col("D", "date", None), "05/01/2016")
    assert parts.coerce(col("T", "datetime2", None), "2026-01-02T03:04") == dt.datetime(2026, 1, 2, 3, 4)
    assert parts.coerce(col("B", "bit", None), "yes") is True


def test_validate_required_and_errors():
    cols = [col("Part Number", nullable=False), col("Comment"), col("Capacitance")]
    with pytest.raises(parts.FormErrors) as e:
        parts.validate(cols, {"Part Number": "", "Comment": "", "Capacitance": "x"})
    assert set(e.value.errors) == {"Part Number", "Comment"}                 # pinned = required
    assert parts.validate(cols, {"Part Number": "A", "Comment": "B", "Capacitance": ""}) == \
        {"Part Number": "A", "Comment": "B", "Capacitance": None}


def test_form_columns_pin_order():
    t = Table("T", False, True, tuple(col(n) for n in
              ["Capacitance", "Footprint Path", "Part Number", "Library Ref", "Comment", "Voltage"]))
    pinned, rest = parts.form_columns(t)
    assert [c.name for c in pinned] == ["Part Number", "Comment", "Library Ref", "Footprint Path"]
    assert [c.name for c in rest] == ["Capacitance", "Voltage"]


# ---------------------------------------------------------------- config

def test_refuses_to_start_without_ui_password():
    base = {"LIBRARIAN_PASSWORD": "x"}
    with pytest.raises(ConfigError, match="PARTSUI_PASSWORD"):
        load_settings(base)
    with pytest.raises(ConfigError):
        load_settings({**base, "PARTSUI_PASSWORD": "   "})
    s = load_settings({**base, "PARTSUI_PASSWORD": "pw", "PARTSUI_LIBS_DIRS": "/libs/a::/libs/b"})
    assert s.libs_dirs == ("/libs/a", "/libs/b") and s.custom_prefix == "Custom - "
    assert "pw" not in repr(s) and "'x'" not in repr(s)


# ---------------------------------------------------------------- auth + CSRF (no DB needed)

@pytest.fixture
def offline_client():
    from app.main import create_app
    s = Settings(db_server="127.0.0.1,1", db_name="nope", db_user="librarian", db_password="x",
                 custom_prefix=PREFIX, ui_password="secret", libs_dirs=())
    with TestClient(create_app(s, scan_libs=False), follow_redirects=False) as c:
        yield c


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_every_route_needs_auth_except_healthz(offline_client):
    app = offline_client.app
    paths = {r.path for r in app.routes if hasattr(r, "methods")}
    assert "/healthz" in paths
    for path in sorted(paths | {"/static/app.css", "/static/htmx.min.js"}):
        method = "POST" if path in {"/parts/save", "/parts/delete", "/reindex", "/libs/rescan",
                                    "/dblib/check", "/dblib/patch"} else "GET"
        r = offline_client.request(method, path)
        if path == "/healthz":
            assert r.status_code == 200 and r.text == "ok"
        else:
            assert r.status_code == 401, path
            assert r.headers["www-authenticate"].startswith("Basic")
    assert offline_client.get("/", headers=_basic("admin", "wrong")).status_code == 401
    assert offline_client.get("/", headers=_basic("root", "secret")).status_code == 401
    assert offline_client.get("/", headers={"Authorization": "Basic !!!"}).status_code == 401


def test_every_post_needs_csrf(offline_client):
    h = _basic("admin", "secret")
    offline_client.get("/healthz")  # no cookie yet
    for path in ["/parts/save", "/parts/delete", "/reindex", "/libs/rescan"]:
        offline_client.cookies.clear()
        r = offline_client.post(path, data={"mode": "new"}, headers=h)
        assert r.status_code == 403, path
        offline_client.cookies.set("partsui_csrf", "B" * 43)
        r = offline_client.post(path, data={"csrf_token": "A" * 43}, headers=h)
        assert r.status_code == 403, path
    for path in ["/dblib/check", "/dblib/patch"]:
        offline_client.cookies.set("partsui_csrf", "B" * 43)
        r = offline_client.post(path, files={"dblib": ("x.DbLib", b"[DatabaseLinks]\r\n")},
                                data={"csrf_token": "A" * 43}, headers=h)
        assert r.status_code == 403, path


def test_headers_and_static(offline_client):
    h = _basic("admin", "secret")
    r = offline_client.get("/static/htmx.min.js", headers=h)
    assert r.status_code == 200 and 'version:"2.0.11"' in r.text
    assert "no-store" not in r.headers.get("cache-control", "")
    r = offline_client.get("/healthz")
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_database_down_gives_a_friendly_503(offline_client):
    r = offline_client.get("/", headers=_basic("admin", "secret"))
    assert r.status_code == 503 and "SQL Server" in r.text
    assert r.headers["cache-control"] == "no-store"
