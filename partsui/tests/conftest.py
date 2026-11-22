"""Test fixtures.

Unit tests need nothing. Integration tests (marked `integration`) need SQL Server:

  * default: start a throwaway mcr.microsoft.com/mssql/server:2022-latest container with
    `docker run` (removed afterwards);
  * or point at an existing THROWAWAY server: PARTSUI_TEST_MSSQL=host,port,
    PARTSUI_TEST_SA_PASSWORD=..., PARTSUI_TEST_MSSQL_THROWAWAY=yes. The tests drop and
    recreate the altium_library database and the altium_reader / librarian logins there.

No docker and no server: integration tests are skipped.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"
SA_PASSWORD = "Test-Sa-Key-2026!"
DB = "altium_library"
VARS = {
    "DB_NAME": DB,
    "READER_USER": "altium_reader", "READER_PASSWORD": "Test-Ro-Key-2026!",
    "LIBRARIAN_USER": "librarian", "LIBRARIAN_PASSWORD": "Test-Rw-Key-2026!",
    "CUSTOM_PREFIX": "Custom - ",
}
UI_PASSWORD = "test-ui-password"
FIXTURE_LIBS = HERE / "fixtures" / "libs"


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs a SQL Server (docker or PARTSUI_TEST_MSSQL)")
    config.addinivalue_line("markers", "slow: long-running (performance)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sa_connect(server: str, password: str, database: str = "master", autocommit: bool = True):
    import pyodbc
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server};DATABASE={database};"
        f"UID=sa;PWD={{{password}}};Encrypt=yes;TrustServerCertificate=yes;Connection Timeout=5",
        autocommit=autocommit)


@pytest.fixture(scope="session")
def mssql():
    """(server, sa_password) of a SQL Server initialised with 01_init.sql, the seed data and
    02_partsui.sql."""
    try:
        import pyodbc  # noqa: F401
    except ImportError:
        pytest.skip("pyodbc not installed")

    container = None
    server = os.environ.get("PARTSUI_TEST_MSSQL")
    password = os.environ.get("PARTSUI_TEST_SA_PASSWORD", SA_PASSWORD)
    if server:
        if os.environ.get("PARTSUI_TEST_MSSQL_THROWAWAY") != "yes":
            pytest.skip("PARTSUI_TEST_MSSQL is set but PARTSUI_TEST_MSSQL_THROWAWAY=yes isn't")
    else:
        if not shutil.which("docker"):
            pytest.skip("no docker and no PARTSUI_TEST_MSSQL")
        port = _free_port()
        container = f"partsui-test-{uuid.uuid4().hex[:8]}"
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", container,
                            "-e", "ACCEPT_EULA=Y", "-e", "MSSQL_PID=Express",
                            "-e", f"MSSQL_SA_PASSWORD={SA_PASSWORD}",
                            "-e", "MSSQL_MEMORY_LIMIT_MB=2048",
                            "-p", f"127.0.0.1:{port}:1433", IMAGE],
                           capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"couldn't start SQL Server container: {r.stderr.strip()[:300]}")
        server, password = f"127.0.0.1,{port}", SA_PASSWORD

    try:
        deadline = time.time() + 120
        while True:
            try:
                conn = _sa_connect(server, password)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(2)
        _reset(conn)
        conn.close()
        yield server, password
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def _reset(master):
    """Fresh database + logins, then the real init scripts and the fake Celestial."""
    from sqlrunner import run_file
    import seed

    cur = master.cursor()
    cur.execute(f"IF DB_ID(N'{DB}') IS NOT NULL BEGIN "
                f"ALTER DATABASE [{DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{DB}]; END")
    for login in (VARS["READER_USER"], VARS["LIBRARIAN_USER"]):
        cur.execute(f"IF SUSER_ID(N'{login}') IS NOT NULL DROP LOGIN [{login}]")
    run_file(master, REPO / "sql" / "01_init.sql", VARS)
    master.cursor().execute(f"USE [{DB}]")
    seed.seed(master)
    run_file(master, REPO / "sql" / "02_partsui.sql", VARS)
    refresh(master)


@pytest.fixture
def sa(mssql):
    server, password = mssql
    conn = _sa_connect(server, password, DB)
    yield conn
    conn.close()


@pytest.fixture
def settings(mssql, tmp_path):
    from app.config import Settings
    server, _ = mssql
    return Settings(db_server=server, db_name=DB, db_user=VARS["LIBRARIAN_USER"],
                    db_password=VARS["LIBRARIAN_PASSWORD"], custom_prefix=VARS["CUSTOM_PREFIX"],
                    ui_password=UI_PASSWORD,
                    libs_dirs=(str(FIXTURE_LIBS / "celestial"), str(FIXTURE_LIBS / "daniel")))


@pytest.fixture
def app(settings, sa):
    from app.main import create_app
    application = create_app(settings, scan_libs=False)
    application.state.scanner.rescan(background=False)
    yield application
    # Undo whatever the test created: drop custom tables (not the template), re-index.
    cur = sa.cursor()
    cur.execute("SELECT name FROM sys.tables WHERE name LIKE N'Custom - %' AND name <> N'Custom - Template'")
    for (name,) in cur.fetchall():
        cur.execute(f"DROP TABLE dbo.[{name.replace(']', ']]')}]")
    refresh(sa)


def refresh(conn):
    """EXEC refresh_parts_index and drain its result set (so the connection stays usable)."""
    cur = conn.cursor()
    cur.execute("EXEC dbo.refresh_parts_index")
    row = cur.fetchone()
    while cur.nextset():
        pass
    conn.commit()
    return tuple(row)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app, follow_redirects=False) as c:
        c.auth = ("admin", UI_PASSWORD)
        r = c.get("/status")
        assert r.status_code == 200
        c.csrf = c.cookies.get("partsui_csrf")
        assert c.csrf
        yield c
