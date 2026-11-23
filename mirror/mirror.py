#!/usr/bin/env python3
"""
Copy every table from the Celestial Azure SQL database into your own SQL Server.

- Tables whose names start with CUSTOM_PREFIX are never touched (yours).
- Celestial's raw z_ tables are skipped. They're the source the category views are built
  from upstream, they're disabled in the DbLib, and the category tables already hold the
  same rows, so copying them would only double the size of the database. A view that
  reads a z_ table can't be recreated without it, so it's copied as a table instead.
- Each table loads into the [staging] schema first, then is swapped into [dbo]
  inside a transaction, so a dropped connection never leaves a half-empty table.
- Re-run any time to pull upstream changes.

Usage (via ./setup.sh mirror):
  mirror.py                       copy everything
  mirror.py --list                show source tables + row counts, copy nothing
  mirror.py --only "Capacitors"   only tables whose name contains the text (repeatable)
  mirror.py --include-raw         also copy the raw z_ tables
"""
import argparse
import os
import sys
import time

import pyodbc

DRIVER = "{ODBC Driver 18 for SQL Server}"


def env(name, default=None, required=True):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"Missing environment variable: {name}")
    return val


def odbc_escape(value: str) -> str:
    # Wrap in {} so ; and = in passwords are safe; double any closing brace.
    return "{" + value.replace("}", "}}") + "}"


def connect(prefix: str, trust_cert: bool) -> pyodbc.Connection:
    parts = [
        f"DRIVER={DRIVER}",
        f"SERVER={env(prefix + '_SERVER')}",
        f"DATABASE={env(prefix + '_DATABASE')}",
        f"UID={odbc_escape(env(prefix + '_USER'))}",
        f"PWD={odbc_escape(env(prefix + '_PASSWORD'))}",
        "Encrypt=yes",
        f"TrustServerCertificate={'yes' if trust_cert else 'no'}",
        "Connection Timeout=30",
    ]
    return pyodbc.connect(";".join(parts), autocommit=False)


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def column_type(row) -> str:
    dtype = row.DATA_TYPE.lower()
    length = row.CHARACTER_MAXIMUM_LENGTH
    if dtype in ("varchar", "nvarchar", "char", "nchar", "varbinary", "binary"):
        return f"{dtype}({'max' if length in (-1, None) else length})"
    if dtype in ("decimal", "numeric"):
        return f"{dtype}({row.NUMERIC_PRECISION},{row.NUMERIC_SCALE})"
    if dtype in ("datetime2", "time", "datetimeoffset") and row.DATETIME_PRECISION is not None:
        return f"{dtype}({row.DATETIME_PRECISION})"
    if dtype in ("text", "ntext"):  # deprecated types
        return "nvarchar(max)" if dtype == "ntext" else "varchar(max)"
    if dtype in ("timestamp", "rowversion"):
        return "varbinary(8)"  # can't insert into rowversion
    if dtype == "sql_variant":
        return "nvarchar(4000)"
    if dtype in ("geography", "geometry", "hierarchyid", "xml"):
        return "nvarchar(max)"
    if dtype == "image":
        return "varbinary(max)"
    return dtype


def select_expr(c) -> str:
    """Read exotic types as text/binary the driver understands."""
    col = q(c.COLUMN_NAME)
    t = c.DATA_TYPE.lower()
    if t == "sql_variant":
        return f"CAST({col} AS nvarchar(4000)) AS {col}"
    if t in ("geography", "geometry", "hierarchyid"):
        return f"{col}.ToString() AS {col}"
    if t == "xml":
        return f"CAST({col} AS nvarchar(max)) AS {col}"
    if t in ("timestamp", "rowversion"):
        return f"CAST({col} AS varbinary(8)) AS {col}"
    return col


def is_raw(name: str) -> bool:
    """Celestial's raw upstream tables (z_CapacitorsCeramic, ...). SQL Server's default
    collation is case-insensitive, so Z_ counts too."""
    return name.lower().startswith("z_")


def wanted(name: str, prefix: str, only: list[str], include_raw: bool = False) -> bool:
    """Should the mirror copy this source table or view?"""
    return (not name.startswith(prefix)
            and not name.startswith("_")
            and name != "sysdiagrams"
            and (include_raw or not is_raw(name))
            and (not only or any(s.lower() in name.lower() for s in only)))


def source_tables(src):
    cur = src.cursor()
    cur.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
    )
    return [(r.TABLE_SCHEMA, r.TABLE_NAME) for r in cur.fetchall()]


def source_columns(src, schema, table):
    cur = src.cursor()
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
        "NUMERIC_SCALE, DATETIME_PRECISION, IS_NULLABLE "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
        "ORDER BY ORDINAL_POSITION",
        schema, table,
    )
    return cur.fetchall()


def widened_type(c) -> str:
    """Views can declare column sizes smaller than the data they actually return,
    so when a view is copied into a table, give text columns room."""
    t = c.DATA_TYPE.lower()
    if t in ("varchar", "char", "text"):
        return "varchar(900)" if c.COLUMN_NAME == "Part Number" else "varchar(max)"
    if t in ("nvarchar", "nchar", "ntext"):
        return "nvarchar(450)" if c.COLUMN_NAME == "Part Number" else "nvarchar(max)"
    return column_type(c)


def drop_object_sql(qualified: str) -> str:
    lit = qualified.replace("'", "''")
    return (
        f"IF OBJECT_ID(N'{lit}', 'V') IS NOT NULL DROP VIEW {qualified}; "
        f"IF OBJECT_ID(N'{lit}', 'U') IS NOT NULL DROP TABLE {qualified};"
    )


def err_text(e: Exception) -> str:
    return " | ".join(str(a) for a in getattr(e, "args", ()) if a) or repr(e)


def copy_table_robust(src, dst, schema, table, batch_size, widen=False):
    """Fast bulk insert first; on failure retry row-by-row (slower, but tolerant of
    odd types/values and gives a real error message)."""
    try:
        return copy_table(src, dst, schema, table, batch_size, fast=True, widen=widen)
    except pyodbc.Error as e:
        dst.rollback()
        try:
            src.rollback()
        except pyodbc.Error:
            pass
        print(f"    fast insert failed ({err_text(e)[:160]}); retrying row-by-row...")
        return copy_table(src, dst, schema, table, batch_size, fast=False, widen=widen)


def copy_table(src, dst, schema, table, batch_size, fast=True, widen=False):
    cols = source_columns(src, schema, table)
    if not cols:
        return 0
    col_defs = ",\n  ".join(
        f"{q(c.COLUMN_NAME)} {widened_type(c) if widen else column_type(c)} "
        f"{'NULL' if c.IS_NULLABLE == 'YES' else 'NOT NULL'}"
        for c in cols
    )
    col_list = ", ".join(q(c.COLUMN_NAME) for c in cols)
    stage = f"staging.{q(table)}"
    final = f"dbo.{q(table)}"

    dcur = dst.cursor()
    dcur.execute(f"IF OBJECT_ID(N'{stage.replace(chr(39), chr(39)*2)}') IS NOT NULL DROP TABLE {stage};")
    dcur.execute(f"CREATE TABLE {stage} (\n  {col_defs}\n);")
    dst.commit()

    scur = src.cursor()
    select_list = ", ".join(select_expr(c) for c in cols)
    scur.execute(f"SELECT {select_list} FROM {q(schema)}.{q(table)}")
    insert_sql = f"INSERT INTO {stage} ({col_list}) VALUES ({', '.join('?' * len(cols))})"
    dcur.fast_executemany = fast

    total = 0
    while True:
        rows = scur.fetchmany(batch_size)
        if not rows:
            break
        dcur.executemany(insert_sql, [tuple(r) for r in rows])
        dst.commit()
        total += len(rows)

    # Swap staging -> dbo in one transaction.
    dcur.execute(drop_object_sql(final))
    dcur.execute(f"ALTER SCHEMA dbo TRANSFER {stage};")
    dst.commit()

    # Index [Part Number] for DbLib lookups, if it exists and is indexable.
    pn = next((c for c in cols if c.COLUMN_NAME == "Part Number"), None)
    pn_len = None if pn is None else (900 if widen and pn.DATA_TYPE.lower() in
                                     ("varchar", "char", "nvarchar", "nchar", "text", "ntext")
                                     else pn.CHARACTER_MAXIMUM_LENGTH)
    if pn and pn_len not in (-1, None) and pn_len <= 900:
        try:
            dcur.execute(f"CREATE INDEX IX_PartNumber ON {final} ([Part Number]);")
            dst.commit()
        except pyodbc.Error as e:
            dst.rollback()
            print(f"    (index skipped: {err_text(e)[:160]})")
    return total


def source_views(src):
    """[(schema, name, definition or None)]. Definition is None if the login can't read it."""
    cur = src.cursor()
    cur.execute(
        "SELECT s.name AS sch, v.name AS vname, OBJECT_DEFINITION(v.object_id) AS vdef "
        "FROM sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id "
        "WHERE v.is_ms_shipped = 0 ORDER BY v.name"
    )
    return [(r.sch, r.vname, r.vdef) for r in cur.fetchall()]


def create_views(dst, views):
    """Recreate views from their definitions. Views may depend on each other, so retry
    until no more progress. Returns {name: error} for the ones that couldn't be created."""
    pending = {name: (schema, d) for schema, name, d in views if d}
    errors = {}
    while pending:
        progress = False
        for name, (schema, d) in list(pending.items()):
            cur = dst.cursor()
            try:
                cur.execute(drop_object_sql(f"dbo.{q(name)}"))
                cur.execute(d)
                dst.commit()
                del pending[name]
                errors.pop(name, None)
                progress = True
            except pyodbc.Error as e:
                dst.rollback()
                errors[name] = err_text(e)[:300]
        if not progress:
            break
    return {n: errors[n] for n in pending}


def refresh_parts_index(dst):
    """Rebuild the Parts Manager search index (sql/02_partsui.sql). Skipped quietly when
    that script hasn't been run; a failure here never fails the mirror."""
    cur = dst.cursor()
    try:
        cur.execute("SELECT OBJECT_ID(N'dbo.refresh_parts_index', N'P')")
        if cur.fetchone()[0] is None:
            return
        t = time.time()
        cur.execute("EXEC dbo.refresh_parts_index")
        row = cur.fetchone()
        while cur.nextset():
            pass
        dst.commit()
        print(f"Parts Manager search index refreshed: {row[0] if row else 0} parts in {time.time() - t:.1f}s.")
    except pyodbc.Error as e:
        dst.rollback()
        print(f"Parts Manager search index NOT refreshed: {err_text(e)[:300]}")
        print("  Use Re-index on the Parts Manager's Status page once the database is healthy.")


def log(dst, table, rows, status):
    cur = dst.cursor()
    cur.execute(
        "INSERT INTO dbo._mirror_log (table_name, row_count, status) VALUES (?, ?, ?)",
        table, rows, status,
    )
    dst.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list source tables and row counts only")
    ap.add_argument("--only", action="append", default=[], help="substring filter on table name")
    ap.add_argument("--views-as-tables", action="store_true",
                    help="copy views as plain tables instead of recreating them")
    ap.add_argument("--include-raw", action="store_true",
                    help="also copy Celestial's raw z_ tables (skipped by default)")
    args = ap.parse_args()

    prefix = env("CUSTOM_PREFIX", "Custom - ")
    batch = int(env("BATCH_SIZE", "2000"))

    print("Connecting to Celestial source...")
    try:
        src = connect("SRC", trust_cert=env("SRC_TRUST_CERT", "no", required=False) == "yes")
    except pyodbc.Error as e:
        msg = err_text(e)
        if "40615" in msg or "firewall" in msg.lower():
            sys.exit("Azure firewall blocked this IP. Add this host's public IP in the Celestial portal.\n" + msg)
        sys.exit(f"Source connection failed: {msg}")

    def pick(name):
        return wanted(name, prefix, args.only, args.include_raw)

    all_tables = source_tables(src)
    tables = [t for t in all_tables if pick(t[1])]
    views = [v for v in source_views(src) if pick(v[1])]
    print(f"{len(tables)} tables and {len(views)} views selected.")
    if not args.include_raw and not args.only:
        raw = sum(1 for t in all_tables if is_raw(t[1]))
        if raw:
            print(f"Skipping {raw} raw z_ tables (nothing uses them; --include-raw copies them).")

    if args.list:
        cur = src.cursor()
        grand = 0
        for schema, table in tables:
            cur.execute(f"SELECT COUNT(*) FROM {q(schema)}.{q(table)}")
            n = cur.fetchone()[0]
            grand += n
            print(f"{n:>8}  {table}")
        print(f"{grand:>8}  TOTAL (tables)")
        readable = sum(1 for v in views if v[2])
        print(f"\nViews: {len(views)} ({readable} with readable definitions)")
        for schema, name, d in views:
            print(f"  {'def ' if d else 'NO-DEF'}  {name}")
        return

    print("Connecting to local server...")
    dst = connect("DST", trust_cert=True)

    # Drop existing local copies of the views first: a schema-bound view would block
    # swapping in the fresh copy of a table it depends on. They are recreated below.
    if views:
        cur = dst.cursor()
        for _, name, d in views:
            cur.execute(drop_object_sql(f"dbo.{q(name)}"))
        dst.commit()

    t0 = time.time()
    failed = []
    grand = 0
    for i, (schema, table) in enumerate(tables, 1):
        t = time.time()
        try:
            n = copy_table_robust(src, dst, schema, table, batch)
            grand += n
            log(dst, table, n, "ok")
            print(f"[{i:>3}/{len(tables)}] {n:>7} rows  {time.time() - t:5.1f}s  {table}")
        except pyodbc.Error as e:
            dst.rollback()
            failed.append(table)
            msg = err_text(e)[:600]
            log(dst, table, 0, "failed")
            print(f"[{i:>3}/{len(tables)}] FAILED  {table}: {msg}")

    # Views: recreate from definitions where possible, otherwise copy their rows as tables.
    if views:
        if args.views_as_tables:
            fallback = [(sch, name) for sch, name, _ in views]
        else:
            print(f"\nRecreating {len(views)} views...")
            view_errors = create_views(dst, views)
            for name in [n for _, n, d in views if d and n not in view_errors]:
                log(dst, name, 0, "view")
            fallback = [(sch, name) for sch, name, d in views if not d or name in view_errors]
            print(f"  {len(views) - len(fallback)} views recreated.")
            for name, err in view_errors.items():
                print(f"  could not recreate '{name}': {err}")
        if fallback:
            print(f"Copying {len(fallback)} view(s) as tables...")
        for i, (schema, name) in enumerate(fallback, 1):
            t = time.time()
            try:
                n = copy_table_robust(src, dst, schema, name, batch, widen=True)
                grand += n
                log(dst, name, n, "view-as-table")
                print(f"[{i:>3}/{len(fallback)}] {n:>7} rows  {time.time() - t:5.1f}s  {name}")
            except pyodbc.Error as e:
                dst.rollback()
                failed.append(name)
                log(dst, name, 0, "failed")
                print(f"[{i:>3}/{len(fallback)}] FAILED  {name}: {err_text(e)[:600]}")

    print(f"\nDone: {grand} rows in {time.time() - t0:.0f}s.")
    # Index whatever is in the database now, including tables that copied fine
    # even if others failed (those keep their previous copy).
    refresh_parts_index(dst)
    if failed:
        print(f"{len(failed)} table(s) failed; their previous copy (if any) was kept:")
        for f in failed:
            print(f"  - {f}")
        print('Retry just those with: ./setup.sh mirror --only "<name>"')
        sys.exit(1)


if __name__ == "__main__":
    main()
