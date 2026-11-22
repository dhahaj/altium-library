"""Database access: connections, the table catalog, identifier quoting and the write guard.

Identifier rules (see README, "Parts Manager"):
  * table and column names only ever come from the catalog below (INFORMATION_SCHEMA),
    are matched exactly against user input, and are quoted with quote_ident();
  * values always travel as ? parameters;
  * every write goes through writable_table(), which only lets CUSTOM_PREFIX tables through.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pyodbc

from .config import Settings

DRIVER = "{ODBC Driver 18 for SQL Server}"

# Shown first in the edit form, and required there.
PINNED_COLUMNS = (
    "Part Number", "Comment", "Description",
    "Library Ref", "Library Path", "Footprint Ref", "Footprint Path",
)
KEY = "Part Number"

TEXT_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
INT_RANGES = {
    "tinyint": (0, 255),
    "smallint": (-2**15, 2**15 - 1),
    "int": (-2**31, 2**31 - 1),
    "bigint": (-2**63, 2**63 - 1),
}
DECIMAL_TYPES = {"decimal", "numeric", "money", "smallmoney"}
FLOAT_TYPES = {"float", "real"}
DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
EDITABLE_TYPES = TEXT_TYPES | set(INT_RANGES) | DECIMAL_TYPES | FLOAT_TYPES | DATE_TYPES | {"bit", "uniqueidentifier"}


class NotWritable(Exception):
    """Raised for any write attempt outside the CUSTOM_PREFIX tables (HTTP 403)."""


class UnknownTable(Exception):
    """A table name that isn't in the catalog (HTTP 404)."""


class UnknownColumn(Exception):
    """A column name that isn't in the table (HTTP 400)."""


def quote_ident(name: str) -> str:
    """[name] with ] doubled, the T-SQL equivalent of QUOTENAME()."""
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError("bad identifier")
    return "[" + name.replace("]", "]]") + "]"


def odbc_escape(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


def connect(settings: Settings, *, autocommit: bool = False) -> pyodbc.Connection:
    parts = [
        f"DRIVER={DRIVER}",
        f"SERVER={settings.db_server}",
        f"DATABASE={odbc_escape(settings.db_name)}",
        f"UID={odbc_escape(settings.db_user)}",
        f"PWD={odbc_escape(settings.db_password)}",
        "Encrypt=yes",
        f"TrustServerCertificate={'yes' if settings.db_trust_cert else 'no'}",
        "APP=Altium Parts Manager",
    ]
    conn = pyodbc.connect(";".join(parts), autocommit=autocommit, timeout=5)  # login timeout
    conn.timeout = 120  # query timeout (seconds); a full re-index takes a few seconds
    return conn


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    max_length: int | None      # characters; -1 = (max); None = not a string type
    precision: int | None
    scale: int | None
    nullable: bool
    has_default: bool
    is_identity: bool
    is_computed: bool
    code_page: int | None       # for char/varchar: the collation's code page

    @property
    def editable(self) -> bool:
        return (self.data_type in EDITABLE_TYPES
                and not self.is_identity and not self.is_computed)

    @property
    def is_unicode(self) -> bool:
        return self.data_type in ("nchar", "nvarchar", "ntext")


@dataclass(frozen=True)
class Table:
    name: str
    is_view: bool
    is_custom: bool
    columns: tuple[Column, ...] = field(repr=False)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise UnknownColumn(name)

    def has(self, name: str) -> bool:
        return any(c.name == name for c in self.columns)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


class Catalog:
    """Every dbo table/view the app cares about, keyed by exact name.

    Part tables: anything with a [Part Number] column, minus raw z_ tables, housekeeping
    tables (starting with _), and the Custom template. The template is kept separately
    because new tables created with @name copy its columns.
    """

    def __init__(self, tables: dict[str, Table], template: Table | None, prefix: str,
                 other_names: list[str] | None = None):
        self.tables = tables
        self.template = template
        self.prefix = prefix
        self.other_names = list(other_names or [])  # hidden tables; they still count for clashes

    def get(self, name: str) -> Table:
        # Exact, case-sensitive match against names that came from the server.
        t = self.tables.get(name) if isinstance(name, str) else None
        if t is None:
            raise UnknownTable(name)
        return t

    def exists_ci(self, name: str) -> bool:
        """SQL Server's default collation is case-insensitive, so clashes are too."""
        low = name.casefold()
        return any(n.casefold() == low for n in self.all_names)

    @property
    def all_names(self) -> list[str]:
        names = list(self.tables)
        if self.template:
            names.append(self.template.name)
        return names + self.other_names

    @property
    def custom(self) -> list[Table]:
        return sorted((t for t in self.tables.values() if t.is_custom), key=lambda t: t.name.casefold())

    @property
    def celestial(self) -> list[Table]:
        return sorted((t for t in self.tables.values() if not t.is_custom), key=lambda t: t.name.casefold())

    def custom_target_for(self, source: Table) -> str:
        """Where a clone of a part from `source` goes."""
        return source.name if source.is_custom else self.prefix + source.name


_CATALOG_SQL = """
SELECT c.TABLE_NAME, t.TABLE_TYPE, c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH,
       c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.DATETIME_PRECISION, c.IS_NULLABLE, c.COLUMN_DEFAULT,
       COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)), c.COLUMN_NAME, 'IsIdentity') AS is_identity,
       COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)), c.COLUMN_NAME, 'IsComputed') AS is_computed,
       CAST(COLLATIONPROPERTY(c.COLLATION_NAME, 'CodePage') AS int) AS code_page
FROM INFORMATION_SCHEMA.COLUMNS c
JOIN INFORMATION_SCHEMA.TABLES t
  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
WHERE c.TABLE_SCHEMA = 'dbo'
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""


def is_hidden_name(name: str) -> bool:
    low = name.lower()
    return low.startswith("z_") or name.startswith("_") or low == "sysdiagrams"


def load_catalog(conn: pyodbc.Connection, prefix: str) -> Catalog:
    cur = conn.cursor()
    cur.execute(_CATALOG_SQL)
    raw: dict[str, tuple[str, list[Column]]] = {}
    for r in cur.fetchall():
        dtype = r.DATA_TYPE.lower()
        prec = r.NUMERIC_PRECISION if dtype in DECIMAL_TYPES | FLOAT_TYPES | set(INT_RANGES) else r.DATETIME_PRECISION
        col = Column(
            name=r.COLUMN_NAME,
            data_type=dtype,
            max_length=r.CHARACTER_MAXIMUM_LENGTH,
            precision=prec,
            scale=r.NUMERIC_SCALE,
            nullable=r.IS_NULLABLE == "YES",
            has_default=r.COLUMN_DEFAULT is not None,
            is_identity=bool(r.is_identity),
            is_computed=bool(r.is_computed),
            code_page=r.code_page if dtype in ("char", "varchar", "text") else None,
        )
        raw.setdefault(r.TABLE_NAME, (r.TABLE_TYPE, []))[1].append(col)

    template_name = prefix + "Template"
    tables: dict[str, Table] = {}
    template = None
    others = []
    for name, (ttype, cols) in raw.items():
        is_view = ttype == "VIEW"
        t = Table(
            name=name,
            is_view=is_view,
            is_custom=(not is_view) and name.startswith(prefix) and name != template_name,
            columns=tuple(cols),
        )
        if name == template_name:
            template = t
        elif is_hidden_name(name) or not t.has(KEY):
            others.append(name)
        else:
            tables[name] = t
    return Catalog(tables, template, prefix, others)


class CatalogCache:
    """The catalog changes rarely (a mirror run, a new custom table), so cache it briefly."""

    def __init__(self, ttl: float = 30.0):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._catalog: Catalog | None = None
        self._loaded_at = 0.0

    def get(self, conn: pyodbc.Connection, prefix: str) -> Catalog:
        with self._lock:
            if self._catalog is None or time.monotonic() - self._loaded_at > self.ttl:
                self._catalog = load_catalog(conn, prefix)
                self._loaded_at = time.monotonic()
            return self._catalog

    def invalidate(self) -> None:
        with self._lock:
            self._catalog = None


def writable_table(catalog: Catalog, name: str) -> Table:
    """The single gate for every INSERT / UPDATE / DELETE.

    Only base tables whose name starts with CUSTOM_PREFIX (and isn't the template) pass.
    Anything else, including names that don't exist, raises NotWritable.
    """
    prefix = catalog.prefix
    if (not isinstance(name, str) or not prefix or not name.startswith(prefix)
            or name == prefix + "Template"):
        raise NotWritable(name)
    t = catalog.tables.get(name)
    if t is None or t.is_view or not t.is_custom:
        raise NotWritable(name)
    return t
