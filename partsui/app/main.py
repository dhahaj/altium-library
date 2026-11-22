"""Parts Manager: a small web UI for adding parts to the Altium DbLib server."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import pyodbc
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import dblib, parts
from .config import ConfigError, Settings, load_settings
from .db import (KEY, PINNED_COLUMNS, Catalog, CatalogCache, NotWritable, Table, UnknownColumn,
                 UnknownTable, connect, writable_table)
from .libscan import LibScanner
from .memform import BadForm, FormTooLarge, parse_multipart
from .security import (CSRF_COOKIE, CSRF_FIELD, CsrfError, OPEN_PATHS, apply_headers,
                       check_basic_auth, csrf_token_for, unauthorized, verify_csrf)

log = logging.getLogger("partsui")
HERE = Path(__file__).parent

NOTICES = {
    "saved": "Saved.",
    "deleted": "Deleted.",
    "created_table": "New table created: {t}. Enable it in your DbLib (see the DbLib page).",
    "index_failed": "Saved, but the search index didn't update. Use Re-index on the Status page.",
    "reindexed": "Search index rebuilt: {n} parts in {s} s.",
    "rescan": "Library rescan started; refresh this page in a moment.",
    "no_libs": "No library folders are configured (PARTSUI_LIBS_DIRS).",
}
# Latest mirror activity, and per table the latest status within 12 h of it.
MIRROR_STATUS_SQL = """
WITH last AS (SELECT MAX(run_at) AS t FROM dbo._mirror_log),
w AS (SELECT l.status,
             ROW_NUMBER() OVER (PARTITION BY l.table_name ORDER BY l.run_at DESC) AS rn
      FROM dbo._mirror_log l CROSS JOIN last
      WHERE l.run_at > DATEADD(hour, -12, last.t))
SELECT (SELECT t FROM last),
       SUM(CASE WHEN status = 'failed' THEN 0 ELSE 1 END),
       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
FROM w WHERE rn = 1
"""
LINK_COLUMN = re.compile(r"^ComponentLink\d*URL$", re.IGNORECASE)
NEW_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-()&+,#]*$")
REF_PATH_PAIRS = {
    "Library Ref": ("Library Path", "sch"),
    "Footprint Ref": ("Footprint Path", "pcb"),
    "Footprint Ref 2": ("Footprint Path 2", "pcb"),
    "Footprint Ref 3": ("Footprint Path 3", "pcb"),
}


class FormProblem(Exception):
    """A bad request we explain on a page (HTTP 400)."""


def create_app(settings: Settings | None = None, *, scan_libs: bool = True) -> FastAPI:
    settings = settings or load_settings()
    scanner = LibScanner(settings.libs_dirs)
    catalog_cache = CatalogCache()

    @asynccontextmanager
    async def lifespan(_app):
        if scan_libs:
            scanner.rescan(background=True)
        yield

    app = FastAPI(title="Parts Manager", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.settings = settings
    app.state.scanner = scanner
    app.state.catalog_cache = catalog_cache
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.trim_blocks = True
    templates.env.lstrip_blocks = True
    templates.env.globals.update(prefix=settings.custom_prefix, link_column=LINK_COLUMN,
                                 pinned=PINNED_COLUMNS, key=KEY, ref_pairs=REF_PATH_PAIRS,
                                 is_required=parts.is_required)

    def urlq(path: str, **params) -> str:
        return path + "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})

    templates.env.globals["urlq"] = urlq
    templates.env.globals["loop_id"] = field_id
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------------ middleware

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path not in OPEN_PATHS:
            if not check_basic_auth(request.headers.get("authorization"), settings.ui_password):
                return unauthorized()
        token, is_new = csrf_token_for(request)
        request.state.csrf = token
        response = await call_next(request)
        if is_new:
            response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="strict", path="/")
        apply_headers(request, response)
        return response

    # ------------------------------------------------------------------ helpers

    def get_conn():
        conn = connect(settings)
        try:
            yield conn
        finally:
            try:
                conn.close()
            except pyodbc.Error:
                pass

    def catalog_of(conn) -> Catalog:
        return catalog_cache.get(conn, settings.custom_prefix)

    def render(request: Request, template: str, /, status_code: int = 200, **ctx) -> HTMLResponse:
        notice = request.query_params.get("notice")
        if notice in NOTICES and "notice" not in ctx:
            try:
                ctx["notice"] = NOTICES[notice].format(t=request.query_params.get("t", ""),
                                                       n=request.query_params.get("n", ""),
                                                       s=request.query_params.get("s", ""))
            except (KeyError, IndexError):
                pass
        ctx.setdefault("libs_enabled", scanner.enabled)
        return templates.TemplateResponse(request, template, {"csrf": request.state.csrf, **ctx},
                                          status_code=status_code)

    def error_page(request: Request, status: int, title: str, message: str) -> HTMLResponse:
        if request.headers.get("hx-request"):
            return HTMLResponse(f'<p class="error">{_esc(message)}</p>', status_code=status)
        return render(request, "error.html", status_code=status, title=title, message=message)

    @app.exception_handler(NotWritable)
    async def _not_writable(request: Request, exc: NotWritable):
        return error_page(request, 403, "Not allowed",
                          f"Only tables starting with “{settings.custom_prefix}” can be changed here. "
                          "Celestial tables are replaced on every mirror run.")

    @app.exception_handler(UnknownTable)
    async def _unknown_table(request: Request, exc: UnknownTable):
        return error_page(request, 404, "Not found", "That table doesn't exist (or isn't a part table).")

    @app.exception_handler(UnknownColumn)
    async def _unknown_column(request: Request, exc: UnknownColumn):
        return error_page(request, 400, "Bad request", "The form names a column that table doesn't have.")

    @app.exception_handler(FormProblem)
    async def _form_problem(request: Request, exc: FormProblem):
        return error_page(request, 400, "Can't do that", str(exc))

    @app.exception_handler(CsrfError)
    async def _csrf(request: Request, exc: CsrfError):
        return error_page(request, 403, "Form expired",
                          "This form is missing its security token. Reload the page and try again.")

    @app.exception_handler(pyodbc.Error)
    async def _db_error(request: Request, exc: pyodbc.Error):
        state = exc.args[0] if exc.args else ""
        # The driver's message names objects and values, never the connection password.
        detail = str(exc.args[1])[:500] if len(exc.args) > 1 else ""
        log.error("database error %s on %s %s: %s", state, request.method, request.url.path, detail)
        if str(state).startswith("08") or state in ("HYT00", "HYT01"):
            return error_page(request, 503, "Database unavailable",
                              "Can't reach SQL Server right now. Is the mssql container running?")
        return error_page(request, 500, "Database error",
                          "SQL Server rejected the request. Details are in the container log.")

    # ------------------------------------------------------------------ health

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return PlainTextResponse("ok")

    # ------------------------------------------------------------------ search

    def search_context(conn, request: Request) -> dict:
        qp = request.query_params
        q = qp.get("q", "")[:200]
        table = qp.get("table", "") or None
        mine = qp.get("mine") in ("1", "on", "true")
        try:
            page = max(1, min(int(qp.get("page", "1")), 10000))
        except ValueError:
            page = 1
        cat = catalog_of(conn)
        if table:
            cat.get(table)  # exact match against the catalog, or 404
        result = None
        short = len(q.strip()) < 2
        if not short or table or mine:
            result = parts.search(conn, "" if short else q, table=table, mine=mine, page=page)
        return dict(q=q, table=table or "", mine=mine, result=result, short=short,
                    celestial=cat.celestial, custom=cat.custom)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, conn=Depends(get_conn)):
        return render(request, "index.html", **search_context(conn, request))

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, conn=Depends(get_conn)):
        ctx = search_context(conn, request)
        if request.headers.get("hx-request") and not request.headers.get("hx-history-restore-request"):
            return render(request, "_results.html", **ctx)
        return render(request, "index.html", **ctx)

    # ------------------------------------------------------------------ part detail

    @app.get("/part", response_class=HTMLResponse)
    def part_detail(request: Request, table: str, pn: str, conn=Depends(get_conn)):
        t = catalog_of(conn).get(table)
        row = parts.get_part(conn, t, pn)
        if row is None:
            return error_page(request, 404, "Not found", f"No part “{pn}” in {table}.")
        fields = [(name, parts.display_value(v)) for name, v in row.items()]
        return render(request, "part.html", table=t, pn=pn, fields=fields)

    # ------------------------------------------------------------------ edit form

    def edit_form(request: Request, *, mode: str, target: str, columns_from: Table, values: dict,
                  create: str = "", create_arg: str = "", original_pn: str = "", old_pn: str = "",
                  source: tuple[str, str] | None = None, errors: dict | None = None,
                  allow_dup: bool = False, status_code: int = 200) -> HTMLResponse:
        pinned, rest = parts.form_columns(columns_from)
        return render(request, "edit.html", status_code=status_code, mode=mode, target=target,
                      pinned_cols=pinned, param_cols=rest, values=values, create=create,
                      create_arg=create_arg, original_pn=original_pn, old_pn=old_pn, source=source,
                      errors=errors or {}, allow_dup=allow_dup, pending=bool(create),
                      like_source=create_arg if create == "like" else "")

    @app.get("/parts/clone", response_class=HTMLResponse)
    def clone_form(request: Request, table: str, pn: str, conn=Depends(get_conn)):
        cat = catalog_of(conn)
        src = cat.get(table)
        row = parts.get_part(conn, src, pn)
        if row is None:
            return error_page(request, 404, "Not found", f"No part “{pn}” in {table}.")
        target = cat.custom_target_for(src)
        if target in cat.tables:
            dest = writable_table(cat, target)
            create, create_arg = "", ""
        else:
            dest, create, create_arg = src, "like", src.name
        values = {c.name: parts.display_value(row.get(c.name)) for c in dest.columns}
        values[KEY] = ""
        return edit_form(request, mode="clone", target=target, columns_from=dest, values=values,
                         create=create, create_arg=create_arg, old_pn=pn, source=(src.name, pn))

    @app.get("/parts/edit", response_class=HTMLResponse)
    def edit_existing(request: Request, table: str, pn: str, conn=Depends(get_conn)):
        cat = catalog_of(conn)
        t = writable_table(cat, table)
        row = parts.get_part(conn, t, pn)
        if row is None:
            return error_page(request, 404, "Not found", f"No part “{pn}” in {table}.")
        values = {c.name: parts.display_value(row.get(c.name)) for c in t.columns}
        return edit_form(request, mode="edit", target=t.name, columns_from=t, values=values,
                         original_pn=pn)

    @app.get("/parts/new", response_class=HTMLResponse)
    def new_part(request: Request, conn=Depends(get_conn)):
        cat = catalog_of(conn)
        qp = request.query_params
        if qp.get("table"):
            t = writable_table(cat, qp["table"])
            return edit_form(request, mode="new", target=t.name, columns_from=t, values={})
        if qp.get("like"):
            src = cat.get(qp["like"])
            target = cat.custom_target_for(src)
            if target in cat.tables:
                t = writable_table(cat, target)
                return edit_form(request, mode="new", target=t.name, columns_from=t, values={})
            return edit_form(request, mode="new", target=target, columns_from=src, values={},
                             create="like", create_arg=src.name)
        if "name" in qp:
            name = " ".join(qp.get("name", "").split())
            problem = new_name_problem(cat, name)
            if problem:
                return render(request, "new.html", status_code=400, celestial=cat.celestial,
                              custom=cat.custom, name=name, name_error=problem)
            return edit_form(request, mode="new", target=settings.custom_prefix + name,
                             columns_from=cat.template, values={}, create="name", create_arg=name)
        return render(request, "new.html", celestial=cat.celestial, custom=cat.custom, name="",
                      name_error=None)

    def new_name_problem(cat: Catalog, name: str) -> str | None:
        if cat.template is None:
            return f"The template table [{settings.template_table}] is missing. Run ./setup.sh init."
        if not name:
            return "Type a name for the new table."
        if len(settings.custom_prefix + name) > 120:
            return "That name is too long."
        if not NEW_NAME.match(name):
            return "Use letters, digits, spaces and - _ ( ) & + , # only."
        if name.casefold() == "template":
            return "That name is reserved."
        if name.casefold().startswith(settings.custom_prefix.strip().casefold()):
            return f"Leave out “{settings.custom_prefix.strip()}”; it's added for you."
        if cat.exists_ci(settings.custom_prefix + name):
            return f"A table called “{settings.custom_prefix + name}” already exists; pick it from the list."
        if cat.exists_ci(name):
            return f"“{name}” is a Celestial category; choose it under “Copy a Celestial category” instead."
        return None

    # ------------------------------------------------------------------ save

    @app.post("/parts/save")
    async def save(request: Request):
        form = await request.form()
        verify_csrf(request, form.get(CSRF_FIELD))
        data = {k: v for k, v in form.items() if isinstance(v, str)}
        return await run_in_threadpool(_save, request, data)

    def _save(request: Request, data: dict[str, str]):
        mode = data.get("mode", "")
        if mode not in ("new", "clone", "edit"):
            raise FormProblem("Unknown form mode.")
        create = data.get("create", "")
        create_arg = data.get("create_arg", "")
        original_pn = data.get("original_pn", "")
        allow_dup = data.get("allow_dup") == "1"
        submitted = {k[4:]: v for k, v in data.items() if k.startswith("col:")}

        conn = connect(settings)
        try:
            cat = catalog_of(conn)
            pending: dict | None = None
            if create == "":
                table = writable_table(cat, data.get("target", ""))
                target, columns_from = table.name, table
            elif create == "like" and mode != "edit":
                src = cat.get(create_arg)
                if src.is_custom:
                    raise FormProblem("That table is already one of yours.")
                target = settings.custom_prefix + src.name
                if target in cat.tables:
                    columns_from, create, create_arg = writable_table(cat, target), "", ""
                else:
                    columns_from, pending = src, {"like": src.name}
            elif create == "name" and mode != "edit":
                name = " ".join(create_arg.split())
                problem = new_name_problem(cat, name)
                if problem:
                    raise FormProblem(problem)
                target, columns_from, pending = settings.custom_prefix + name, cat.template, {"name": name}
            else:
                raise FormProblem("Unknown target table.")

            editable = {c.name: c for c in columns_from.columns if c.editable}
            for name in submitted:
                if name not in editable:
                    raise UnknownColumn(name)
            # Validate what was submitted, plus anything required that wasn't.
            cols = [c for c in columns_from.columns if c.editable
                    and (c.name in submitted or parts.is_required(c))]

            def again(errors: dict, status: int = 422):
                return edit_form(request, mode=mode, target=target, columns_from=columns_from,
                                 values=submitted, create=create, create_arg=create_arg,
                                 original_pn=original_pn, old_pn=data.get("old_pn", ""),
                                 source=_source(data), errors=errors, allow_dup=allow_dup,
                                 status_code=status)

            try:
                values = parts.validate(cols, submitted)
            except parts.FormErrors as e:
                return again(e.errors)

            pn = values[KEY]
            hits = parts.where_else(conn, cat, pn,
                                    exclude=(target, original_pn) if mode == "edit" else None)
            mine = [t for t, custom in hits if custom]
            theirs = [t for t, custom in hits if not custom]
            if mode == "edit" and pn.casefold() == original_pn.casefold():
                theirs = []  # accepted when the part was first saved
            if mine:
                return again({KEY: f"“{pn}” already exists in {', '.join(mine)}."}, 409)
            if theirs and not allow_dup:
                return again({KEY: f"“{pn}” is already a Celestial part ({', '.join(theirs[:3])}"
                                   f"{'…' if len(theirs) > 3 else ''}). Tick “allow duplicate” "
                                   "below to add your own copy anyway."}, 409)

            created = None
            if pending:
                created = parts.make_custom_table(conn, **pending)
                conn.commit()
                catalog_cache.invalidate()
                cat = catalog_of(conn)
                table = writable_table(cat, created)
                target, columns_from, create, create_arg = table.name, table, "", ""

            try:
                if mode == "edit":
                    parts.update_part(conn, cat, target, original_pn, values)
                else:
                    parts.insert_part(conn, cat, target, values)
                conn.commit()
            except pyodbc.IntegrityError:
                conn.rollback()
                return again({KEY: f"“{pn}” already exists in {target}."}, 409)
            except LookupError:
                conn.rollback()
                raise FormProblem(f"“{original_pn}” is no longer in {target}; someone changed it.")

            notice, extra = ("created_table", {"t": created}) if created else ("saved", {})
            try:
                if mode == "edit" and original_pn != pn:
                    parts.reindex_part(conn, target, original_pn)
                parts.reindex_part(conn, target, pn)
                conn.commit()
            except pyodbc.Error:
                conn.rollback()
                log.exception("index update failed")
                notice = "index_failed"
            return RedirectResponse(urlq("/part", table=target, pn=pn, notice=notice, **extra),
                                    status_code=303)
        finally:
            conn.close()

    def _source(data: dict) -> tuple[str, str] | None:
        if data.get("source_table") and data.get("source_pn"):
            return data["source_table"], data["source_pn"]
        return None

    # ------------------------------------------------------------------ delete

    @app.post("/parts/delete")
    async def delete(request: Request):
        form = await request.form()
        verify_csrf(request, form.get(CSRF_FIELD))
        table, pn = str(form.get("table", "")), str(form.get("pn", ""))
        return await run_in_threadpool(_delete, request, table, pn)

    def _delete(request: Request, table: str, pn: str):
        conn = connect(settings)
        try:
            cat = catalog_of(conn)
            writable_table(cat, table)
            n = parts.delete_part(conn, cat, table, pn)
            conn.commit()
            if n == 0:
                return error_page(request, 404, "Not found", f"No part “{pn}” in {table}.")
            try:
                parts.reindex_part(conn, table, pn)
                conn.commit()
            except pyodbc.Error:
                conn.rollback()
                log.exception("index update failed")
            return RedirectResponse(urlq("/", mine="1", notice="deleted"), status_code=303)
        finally:
            conn.close()

    # ------------------------------------------------------------------ library autocomplete

    @app.get("/libs/suggest", response_class=HTMLResponse)
    def suggest(request: Request, kind: str = "", field: str = "", col: str = "", q: str = ""):
        """Autocomplete for Library Ref / Footprint Ref. The input sends its own value as
        col:<column>; `field` is the input's element id, echoed back for the click handler."""
        if kind not in ("sch", "pcb") or not re.fullmatch(r"f-[a-z0-9-]{1,80}", field or "f-x"):
            return HTMLResponse("", status_code=400)
        q = q or request.query_params.get("col:" + col, "")
        items = scanner.index.suggest(kind, q[:200], limit=20) if q.strip() else []
        return render(request, "_suggest.html", items=items, field=field, q=q)

    @app.post("/libs/rescan")
    async def rescan(request: Request):
        form = await request.form()
        verify_csrf(request, form.get(CSRF_FIELD))
        if not scanner.enabled:
            return RedirectResponse(urlq("/status", notice="no_libs"), status_code=303)
        scanner.rescan(background=True)
        return RedirectResponse(urlq("/status", notice="rescan"), status_code=303)

    # ------------------------------------------------------------------ status

    def status_context(conn_factory) -> dict:
        ctx: dict = {"db_ok": False, "db_error": None, "celestial_parts": None, "my_parts": None,
                     "last_mirror": None, "index_rows": None, "custom_tables": None}
        try:
            conn = conn_factory()
        except pyodbc.Error as e:
            ctx["db_error"] = str(e.args[0]) if e.args else "connection failed"
            return ctx
        try:
            cur = conn.cursor()
            cur.execute("SELECT is_custom, COUNT(*) FROM dbo._parts_index GROUP BY is_custom")
            counts = {bool(k): n for k, n in cur.fetchall()}
            ctx.update(db_ok=True, celestial_parts=counts.get(False, 0), my_parts=counts.get(True, 0))
            cur.execute(MIRROR_STATUS_SQL)
            row = cur.fetchone()
            if row and row[0] is not None:
                ctx.update(last_mirror=row[0], mirror_ok=row[1] or 0, mirror_failed=row[2] or 0)
            ctx["custom_tables"] = len(catalog_of(conn).custom)
        except pyodbc.Error as e:
            ctx["db_error"] = str(e.args[0]) if e.args else "query failed"
        finally:
            conn.close()
        return ctx

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request):
        return render(request, "status.html", scanner=scanner, idx=scanner.index,
                      **status_context(lambda: connect(settings)))

    @app.get("/status/mini", response_class=HTMLResponse)
    def status_mini(request: Request):
        return render(request, "_status_mini.html", scanner=scanner, idx=scanner.index,
                      **status_context(lambda: connect(settings)))

    @app.post("/reindex")
    async def reindex(request: Request):
        form = await request.form()
        verify_csrf(request, form.get(CSRF_FIELD))

        def run():
            conn = connect(settings)
            try:
                t0 = time.monotonic()
                n, _ = parts.refresh_index(conn)
                conn.commit()
                return n, time.monotonic() - t0
            finally:
                conn.close()

        n, secs = await run_in_threadpool(run)
        return RedirectResponse(urlq("/status", notice="reindexed", n=str(n), s=f"{secs:.1f}"),
                                status_code=303)

    # ------------------------------------------------------------------ DbLib

    def custom_tables_with_columns() -> list[Table]:
        conn = connect(settings)
        try:
            return catalog_of(conn).custom
        finally:
            conn.close()

    @app.get("/dblib", response_class=HTMLResponse)
    def dblib_page(request: Request):
        tables = custom_tables_with_columns()
        return render(request, "dblib.html", tables=tables, rows=None, dblib_error=None)

    async def _dblib_upload(request: Request) -> tuple[bytes, str]:
        try:
            fields, files = await parse_multipart(request)
        except FormTooLarge:
            raise FormProblem("That file is too big to be a .DbLib (8 MB limit).") from None
        except BadForm:
            raise FormProblem("Upload the .DbLib file with the form on the DbLib page.") from None
        verify_csrf(request, fields.get(CSRF_FIELD))
        f = files.get("dblib")
        if f is None or not f.data:
            raise FormProblem("Choose your .DbLib file first.")
        return f.data, f.filename

    def dblib_rows(info: dblib.DbLibInfo, tables: list[Table]) -> list[dict]:
        rows = []
        for t in tables:
            entry = info.find(t.name)
            rows.append({"name": t.name,
                         "state": "missing" if entry is None else ("enabled" if entry.enabled else "disabled"),
                         "number": entry.number if entry else None})
        return rows

    @app.post("/dblib/check", response_class=HTMLResponse)
    async def dblib_check(request: Request):
        data, _ = await _dblib_upload(request)
        tables = await run_in_threadpool(custom_tables_with_columns)
        try:
            info = dblib.parse(data)
        except dblib.DbLibError as e:
            return render(request, "_dblib_status.html", rows=None, dblib_error=str(e))
        finally:
            del data
        return render(request, "_dblib_status.html", rows=dblib_rows(info, tables), dblib_error=None,
                      total_tables=len(info.tables))

    @app.post("/dblib/patch")
    async def dblib_patch(request: Request):
        data, filename = await _dblib_upload(request)
        tables = await run_in_threadpool(custom_tables_with_columns)
        try:
            patched, added = dblib.patch(data, [(t.name, t.column_names) for t in tables])
        except dblib.DbLibError as e:
            raise FormProblem(str(e)) from None
        finally:
            del data
        base = os.path.basename(filename.replace("\\", "/")) or "Daniel.DbLib"
        if not re.fullmatch(r"[A-Za-z0-9 ._()-]{1,100}\.[Dd][Bb][Ll][Ii][Bb]", base):
            base = "Patched.DbLib"
        log.info("DbLib patched: %d table(s) added", len(added))
        return Response(patched, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{base}"',
                                 "X-Tables-Added": str(len(added))})

    return app


def field_id(column: str) -> str:
    """A stable, HTML-safe element id for a column name."""
    slug = re.sub(r"[^a-z0-9]+", "-", column.lower()).strip("-")[:40] or "col"
    return f"{slug}-{hashlib.sha1(column.encode()).hexdigest()[:6]}"


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def build() -> FastAPI:
    """Entry point for uvicorn --factory."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(2) from None
    return create_app(settings)
