"""Queries and writes for parts. Every SQL string here is built from quote_ident()-quoted
catalog names only; every value is a ? parameter."""
from __future__ import annotations

import datetime as dt
import decimal
import uuid
from dataclasses import dataclass

import pyodbc

from .db import (DATE_TYPES, DECIMAL_TYPES, FLOAT_TYPES, INT_RANGES, KEY, PINNED_COLUMNS,
                 TEXT_TYPES, Catalog, Column, Table, quote_ident, writable_table)

PAGE_SIZE = 50
MAX_TERMS = 6
_BIN = "Latin1_General_100_BIN2"


# ---------------------------------------------------------------------------- search

def like_escape(text: str) -> str:
    """Escape LIKE wildcards; used with ESCAPE '\\'."""
    return (text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                .replace("[", "\\["))


@dataclass
class SearchResult:
    rows: list
    page: int
    has_next: bool


def search(conn: pyodbc.Connection, q: str, *, table: str | None = None, mine: bool = False,
           page: int = 1) -> SearchResult:
    """Search _parts_index. Every whitespace-separated term must match part number, comment
    or description; rows whose part number starts with the whole query rank first."""
    q = " ".join(q.split())
    terms = q.split(" ")[:MAX_TERMS] if q else []
    where, params = [], []
    for term in terms:
        pat = "%" + like_escape(term) + "%"
        # Contains-matches can't use an index anyway. Comparing UPPER()ed text under a
        # binary collation skips the case-insensitive Unicode rules and is ~10x faster than
        # a plain LIKE at 260k rows (0.2-0.4 s instead of ~2 s on SQL Server Express).
        where.append("(" + " OR ".join(
            f"UPPER({c}) COLLATE {_BIN} LIKE UPPER(?) COLLATE {_BIN} ESCAPE '\\'"
            for c in ("part_number", "comment", "description")) + ")")
        params += [pat, pat, pat]
    if table:
        where.append("table_name = ?")
        params.append(table)
    if mine:
        where.append("is_custom = 1")
    page = max(1, page)
    sql = (
        "SELECT table_name, part_number, comment, description, footprint_ref, is_custom "
        "FROM dbo._parts_index "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + "ORDER BY CASE WHEN part_number LIKE CAST(? AS varchar(902)) ESCAPE '\\' THEN 0 ELSE 1 END, "
          "part_number, table_name "
          "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
    )
    params += [like_escape(q) + "%", (page - 1) * PAGE_SIZE, PAGE_SIZE + 1]
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    return SearchResult(rows=rows[:PAGE_SIZE], page=page, has_next=len(rows) > PAGE_SIZE)


# ---------------------------------------------------------------------------- reading

def _key_predicate(table: Table) -> str:
    col = table.column(KEY)
    if col.data_type in ("char", "varchar") and col.max_length not in (None, -1):
        # Bind as varchar so the [Part Number] index is usable (no implicit nvarchar convert).
        return f"{quote_ident(KEY)} = CAST(? AS varchar({col.max_length}))"
    return f"{quote_ident(KEY)} = ?"


def get_part(conn: pyodbc.Connection, table: Table, part_number: str) -> dict | None:
    cur = conn.cursor()
    cur.execute(f"SELECT TOP (1) * FROM dbo.{quote_ident(table.name)} WHERE {_key_predicate(table)}",
                part_number)
    row = cur.fetchone()
    if row is None:
        return None
    names = [d[0] for d in cur.description]
    return dict(zip(names, row))


def display_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return str(value)


def where_else(conn: pyodbc.Connection, catalog: Catalog, part_number: str,
               exclude: tuple[str, str] | None = None) -> list[tuple[str, bool]]:
    """Every (table, is_custom) that already holds part_number, from the index plus a direct
    look at the custom tables (in case the index is stale). `exclude` = (table, part_number)
    of the row being edited."""
    found: dict[str, bool] = {}
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT table_name, is_custom FROM dbo._parts_index "
                "WHERE part_number = CAST(? AS varchar(900))", part_number)
    for name, is_custom in cur.fetchall():
        found[name] = bool(is_custom)
    for t in catalog.custom:
        if t.name in found:
            continue
        cur.execute(f"SELECT TOP (1) 1 FROM dbo.{quote_ident(t.name)} WHERE {_key_predicate(t)}", part_number)
        if cur.fetchone():
            found[t.name] = True
    if exclude and exclude[1].casefold() == part_number.casefold():
        found.pop(exclude[0], None)
    return sorted(found.items(), key=lambda kv: (not kv[1], kv[0]))


# ---------------------------------------------------------------------------- validation

class FormErrors(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def form_columns(table: Table) -> tuple[list[Column], list[Column]]:
    """(pinned, parameters): editable columns, pinned ones first in PINNED_COLUMNS order,
    the rest in table column order."""
    editable = [c for c in table.columns if c.editable]
    by_name = {c.name: c for c in editable}
    pinned = [by_name[n] for n in PINNED_COLUMNS if n in by_name]
    rest = [c for c in editable if c.name not in PINNED_COLUMNS]
    return pinned, rest


def is_required(col: Column) -> bool:
    return col.name in PINNED_COLUMNS or (not col.nullable and not col.has_default)


def _text_len(col: Column, value: str) -> int:
    if col.is_unicode:
        return len(value.encode("utf-16-le")) // 2
    return len(value.encode(_codec(col), errors="replace"))


def _codec(col: Column) -> str:
    cp = col.code_page or 1252
    return "utf-8" if cp == 65001 else f"cp{cp}"


def coerce(col: Column, raw: str):
    """Turn a form string into the value to bind for `col`. Raises ValueError with a
    human-readable reason. '' means NULL."""
    value = raw.strip()
    if value == "":
        return None
    t = col.data_type
    if t in TEXT_TYPES:
        if not col.is_unicode:
            try:
                value.encode(_codec(col))
            except (UnicodeEncodeError, LookupError):
                bad = next(ch for ch in value if not _encodable(ch, _codec(col)))
                raise ValueError(f"contains “{bad}”, which this column can't store; spell it out "
                                 f"(e.g. Ohm, u) instead") from None
        if col.max_length not in (None, -1) and _text_len(col, value) > col.max_length:
            raise ValueError(f"is {_text_len(col, value)} characters; the column holds {col.max_length}")
        return value
    if t in INT_RANGES:
        try:
            n = int(value)
        except ValueError:
            raise ValueError("must be a whole number") from None
        lo, hi = INT_RANGES[t]
        if not lo <= n <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return n
    if t in DECIMAL_TYPES:
        try:
            d = decimal.Decimal(value)
        except decimal.InvalidOperation:
            raise ValueError("must be a number") from None
        if not d.is_finite():
            raise ValueError("must be a number")
        if col.precision and col.scale is not None and t in ("decimal", "numeric"):
            _, digits, exp = d.normalize().as_tuple() if d != 0 else (0, (0,), 0)
            int_digits = max(len(digits) + exp, 0)
            frac_digits = max(-exp, 0)
            if int_digits > col.precision - col.scale or frac_digits > col.scale:
                raise ValueError(f"must fit {col.precision - col.scale} digits before and "
                                 f"{col.scale} after the decimal point")
        return d
    if t in FLOAT_TYPES:
        try:
            f = float(value)
        except ValueError:
            raise ValueError("must be a number") from None
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("must be a number")
        return f
    if t == "bit":
        low = value.lower()
        if low in ("1", "true", "yes", "y", "t"):
            return True
        if low in ("0", "false", "no", "n", "f"):
            return False
        raise ValueError("must be True or False")
    if t == "uniqueidentifier":
        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise ValueError("must be a GUID") from None
    if t in DATE_TYPES:
        try:
            if t == "date":
                return dt.date.fromisoformat(value)
            if t == "time":
                return dt.time.fromisoformat(value)
            parsed = dt.datetime.fromisoformat(value.replace("T", " "))
            if t == "datetimeoffset":
                return parsed.isoformat(sep=" ")
            return parsed
        except ValueError:
            fmt = {"date": "YYYY-MM-DD", "time": "HH:MM[:SS]"}.get(t, "YYYY-MM-DD HH:MM[:SS]")
            raise ValueError(f"must be a date like {fmt}") from None
    raise ValueError("this column type can't be edited here")


def _encodable(ch: str, codec: str) -> bool:
    try:
        ch.encode(codec)
        return True
    except UnicodeEncodeError:
        return False


def validate(table_columns: list[Column], form: dict[str, str]) -> dict[str, object]:
    """form maps column name -> raw text for every editable column. Returns column -> value."""
    errors, values = {}, {}
    for col in table_columns:
        raw = form.get(col.name, "")
        try:
            v = coerce(col, raw)
        except ValueError as e:
            errors[col.name] = str(e)
            continue
        if v is None and is_required(col):
            errors[col.name] = "is required"
            continue
        values[col.name] = v
    if errors:
        raise FormErrors(errors)
    return values


# ---------------------------------------------------------------------------- writing

def insert_part(conn: pyodbc.Connection, catalog: Catalog, table_name: str, values: dict) -> None:
    table = writable_table(catalog, table_name)
    cols = [table.column(n) for n in values]  # raises UnknownColumn for anything odd
    sql = (f"INSERT INTO dbo.{quote_ident(table.name)} ({', '.join(quote_ident(c.name) for c in cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)})")
    conn.cursor().execute(sql, [values[c.name] for c in cols])


def update_part(conn: pyodbc.Connection, catalog: Catalog, table_name: str, original_pn: str,
                values: dict) -> None:
    table = writable_table(catalog, table_name)
    cols = [table.column(n) for n in values]
    sql = (f"UPDATE dbo.{quote_ident(table.name)} SET "
           + ", ".join(f"{quote_ident(c.name)} = ?" for c in cols)
           + f" WHERE {_key_predicate(table)}")
    cur = conn.cursor()
    cur.execute(sql, [values[c.name] for c in cols] + [original_pn])
    if cur.rowcount != 1:
        raise LookupError(f"expected to update 1 row, matched {cur.rowcount}")


def delete_part(conn: pyodbc.Connection, catalog: Catalog, table_name: str, part_number: str) -> int:
    table = writable_table(catalog, table_name)
    cur = conn.cursor()
    cur.execute(f"DELETE FROM dbo.{quote_ident(table.name)} WHERE {_key_predicate(table)}", part_number)
    return cur.rowcount


def reindex_part(conn: pyodbc.Connection, table_name: str, part_number: str) -> None:
    conn.cursor().execute("EXEC dbo.upsert_parts_index_row @table = ?, @part_number = ?",
                          table_name, part_number)


def refresh_index(conn: pyodbc.Connection) -> tuple[int, int]:
    cur = conn.cursor()
    cur.execute("EXEC dbo.refresh_parts_index")
    row = cur.fetchone()
    while cur.nextset():
        pass
    return (row[0], row[1]) if row else (0, 0)


def make_custom_table(conn: pyodbc.Connection, *, like: str | None = None, name: str | None = None) -> str:
    cur = conn.cursor()
    if like is not None:
        cur.execute("EXEC dbo.make_custom_table @like = ?", like)
    else:
        cur.execute("EXEC dbo.make_custom_table @name = ?", name)
    row = cur.fetchone()
    while cur.nextset():
        pass
    return row[0]
