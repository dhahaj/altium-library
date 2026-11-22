"""Run the repo's .sql files the way `sqlcmd -i file -v VAR=value` does: substitute $(VAR)
and split batches on GO lines. Used by the integration tests, so they exercise the real
01_init.sql / 02_partsui.sql with the real logins and permissions."""
from __future__ import annotations

import re
from pathlib import Path

import pyodbc

GO = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)
VAR = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


def batches(sql: str, variables: dict[str, str]) -> list[str]:
    def sub(m):
        if m.group(1) not in variables:
            raise KeyError(f"sqlcmd variable $({m.group(1)}) not set")
        return variables[m.group(1)]
    text = VAR.sub(sub, sql)
    return [b for b in GO.split(text) if b.strip()]


def run_file(conn: pyodbc.Connection, path: Path, variables: dict[str, str]) -> None:
    cur = conn.cursor()
    for batch in batches(path.read_text(encoding="utf-8"), variables):
        cur.execute(batch)
        while True:
            try:
                if not cur.nextset():
                    break
            except pyodbc.ProgrammingError:
                break
    conn.commit()
