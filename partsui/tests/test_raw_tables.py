"""Celestial's raw z_ tables: the mirror skips them, and sql/03_drop_raw_tables.sql removes
the ones an older mirror copied in. The SQL tests use their own scratch database, so they
don't disturb the fake Celestial the other integration tests share."""
from __future__ import annotations

import importlib.util

import pyodbc
import pytest

from conftest import REPO, _sa_connect
from sqlrunner import batches

SCRATCH_DB = "zdrop_test"
SCRIPT = REPO / "sql" / "03_drop_raw_tables.sql"


def _load_mirror():
    spec = importlib.util.spec_from_file_location("mirror_under_test", REPO / "mirror" / "mirror.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mirror = _load_mirror()


# --- mirror table filter (no database) -------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("Capacitors - Ceramic - 0603", True),
    ("Memory", True),
    ("z_CapacitorsCeramic", False),
    ("Z_Memory", False),             # SQL Server names are case-insensitive
    ("Custom - Capacitors", False),
    ("_mirror_log", False),
    ("sysdiagrams", False),
    ("Fuzz_Box", True),              # z_ only counts as a prefix
])
def test_mirror_skips_raw_tables_by_default(name, expected):
    assert mirror.wanted(name, "Custom - ", []) is expected


def test_mirror_include_raw_and_only():
    assert mirror.wanted("z_CapacitorsCeramic", "Custom - ", [], include_raw=True)
    # The restore command from the README: --include-raw --only z_
    assert mirror.wanted("z_Memory", "Custom - ", ["z_"], include_raw=True)
    assert not mirror.wanted("Memory", "Custom - ", ["z_"], include_raw=True)
    # --include-raw never overrides the other exclusions.
    assert not mirror.wanted("Custom - z_Mine", "Custom - ", [], include_raw=True)


# --- sql/03_drop_raw_tables.sql against a real SQL Server ------------------------------

def run_script(conn):
    """Run the script batch by batch like `sqlcmd -b`: stop at the first error and return
    (error or None, printed messages)."""
    cur = conn.cursor()
    messages = []
    for batch in batches(SCRIPT.read_text(encoding="utf-8"), {"DB_NAME": SCRATCH_DB}):
        try:
            cur.execute(batch)
            messages += [m for _, m in cur.messages]
            while cur.nextset():
                messages += [m for _, m in cur.messages]
        except pyodbc.Error as e:
            return e, messages
    return None, messages


def dbo_tables(conn):
    cur = conn.cursor()
    cur.execute(f"SELECT name FROM [{SCRATCH_DB}].sys.tables WHERE schema_id = 1 ORDER BY name")
    return [r.name for r in cur.fetchall()]


@pytest.fixture
def scratch(mssql):
    server, password = mssql
    conn = _sa_connect(server, password)
    cur = conn.cursor()

    def drop_db():
        cur.execute("USE master")
        cur.execute(f"IF DB_ID(N'{SCRATCH_DB}') IS NOT NULL BEGIN "
                    f"ALTER DATABASE [{SCRATCH_DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                    f"DROP DATABASE [{SCRATCH_DB}]; END")

    drop_db()
    cur.execute(f"CREATE DATABASE [{SCRATCH_DB}]")
    cur.execute(f"USE [{SCRATCH_DB}]")
    for sql in [
        "CREATE TABLE dbo.z_CapacitorsCeramic (partid int PRIMARY KEY, partnumber varchar(200))",
        "INSERT INTO dbo.z_CapacitorsCeramic VALUES (1, 'GRM188R71H104KA93D')",
        # A foreign key and a trigger between z_ tables don't count as outside users.
        "CREATE TABLE dbo.z_Memory (partid int PRIMARY KEY, capref int "
        "REFERENCES dbo.z_CapacitorsCeramic (partid))",
        "CREATE TABLE dbo.Z_UpperCase (partid int)",
        """CREATE TRIGGER dbo.trg_z ON dbo.z_Memory AFTER INSERT AS
           SELECT COUNT(*) FROM dbo.z_CapacitorsCeramic""",
        "CREATE TABLE dbo.[Capacitors - Ceramic - 0603] ([Part Number] varchar(200) PRIMARY KEY)",
        "INSERT INTO dbo.[Capacitors - Ceramic - 0603] VALUES ('GRM188R71H104KA93D')",
        "CREATE TABLE dbo.[Custom - Template] ([Part Number] varchar(200) PRIMARY KEY)",
        "CREATE TABLE dbo.Fuzz_Box (id int)",
    ]:
        cur.execute(sql)
    yield conn
    drop_db()
    conn.close()


KEEP = ["Capacitors - Ceramic - 0603", "Custom - Template", "Fuzz_Box"]
RAW = ["z_CapacitorsCeramic", "z_Memory", "Z_UpperCase"]


@pytest.mark.integration
def test_drop_raw_refuses_while_a_view_uses_them(scratch):
    scratch.cursor().execute("CREATE VIEW dbo.[Capacitors - Ceramic - 0402] AS "
                             "SELECT partnumber AS [Part Number] FROM dbo.z_CapacitorsCeramic")
    err, _ = run_script(scratch)
    assert err is not None and "Nothing dropped" in str(err)
    assert sorted(dbo_tables(scratch), key=str.lower) == sorted(KEEP + RAW, key=str.lower)


@pytest.mark.integration
def test_drop_raw_refuses_while_a_foreign_key_points_at_them(scratch):
    scratch.cursor().execute("ALTER TABLE dbo.Fuzz_Box ADD CONSTRAINT FK_fuzz "
                             "FOREIGN KEY (id) REFERENCES dbo.z_CapacitorsCeramic (partid)")
    err, _ = run_script(scratch)
    assert err is not None and "Nothing dropped" in str(err)
    assert set(RAW) <= set(dbo_tables(scratch))


@pytest.mark.integration
def test_drop_raw_drops_only_z_tables_and_is_rerunnable(scratch):
    err, messages = run_script(scratch)
    assert err is None, err
    assert any("Dropped 3 z_ tables" in m for m in messages), messages
    assert sorted(dbo_tables(scratch), key=str.lower) == sorted(KEEP, key=str.lower)
    cur = scratch.cursor()
    cur.execute(f"SELECT COUNT(*) FROM [{SCRATCH_DB}].dbo.[Capacitors - Ceramic - 0603]")
    assert cur.fetchone()[0] == 1

    err, messages = run_script(scratch)
    assert err is None, err
    assert any("Nothing to do" in m for m in messages), messages
