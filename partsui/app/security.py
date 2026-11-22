"""HTTP Basic auth, CSRF tokens and response headers."""
from __future__ import annotations

import base64
import binascii
import re
import secrets

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

USERNAME = "admin"
CSRF_COOKIE = "partsui_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
OPEN_PATHS = {"/healthz"}

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def check_basic_auth(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, pw = decoded.partition(":")
    if not sep:
        return False
    ok_user = secrets.compare_digest(user.encode(), USERNAME.encode())
    ok_pw = secrets.compare_digest(pw.encode(), password.encode())
    return ok_user and ok_pw


def unauthorized() -> Response:
    return PlainTextResponse("Authentication required.", status_code=401,
                             headers={"WWW-Authenticate": 'Basic realm="Parts Manager", charset="UTF-8"',
                                      "Cache-Control": "no-store"})


def csrf_token_for(request: Request) -> tuple[str, bool]:
    """(token, is_new). The token lives in an HttpOnly, SameSite=Strict cookie and is echoed
    into every form; a POST must carry the same value (double-submit)."""
    tok = request.cookies.get(CSRF_COOKIE, "")
    if _TOKEN_RE.match(tok):
        return tok, False
    return secrets.token_urlsafe(32), True


class CsrfError(Exception):
    pass


def verify_csrf(request: Request, submitted: str | None) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    submitted = submitted or request.headers.get(CSRF_HEADER, "")
    if not (_TOKEN_RE.match(cookie) and submitted
            and secrets.compare_digest(cookie.encode(), submitted.encode())):
        raise CsrfError()


def apply_headers(request: Request, response: Response) -> None:
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", CSP)
