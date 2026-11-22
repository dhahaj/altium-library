"""Parse a multipart/form-data body entirely in memory.

Starlette's form parser spools uploads larger than 1 MB to a temporary file. A .DbLib holds
the reader password, so the DbLib routes read the body here instead: capped in size, kept in
RAM, never written anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

MAX_BODY = 8 * 1024 * 1024


class FormTooLarge(Exception):
    pass


class BadForm(Exception):
    pass


@dataclass
class MemFile:
    filename: str
    data: bytes = field(repr=False)


async def read_body(request: Request, limit: int = MAX_BODY) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise FormTooLarge()
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise FormTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


async def parse_multipart(request: Request, limit: int = MAX_BODY) -> tuple[dict[str, str], dict[str, MemFile]]:
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype != b"multipart/form-data" or b"boundary" not in params:
        raise BadForm("expected multipart/form-data")
    body = await read_body(request, limit)

    fields: dict[str, str] = {}
    files: dict[str, MemFile] = {}
    state = {"headers": {}, "hname": b"", "hval": b"", "data": bytearray()}

    def on_part_begin():
        state["headers"], state["data"] = {}, bytearray()

    def on_header_field(data, start, end):
        state["hname"] += data[start:end]

    def on_header_value(data, start, end):
        state["hval"] += data[start:end]

    def on_header_end():
        state["headers"][state["hname"].lower()] = state["hval"]
        state["hname"], state["hval"] = b"", b""

    def on_part_data(data, start, end):
        state["data"].extend(data[start:end])

    def on_part_end():
        _, opts = parse_options_header(state["headers"].get(b"content-disposition", b""))
        name = opts.get(b"name", b"").decode("utf-8", "replace")
        if not name:
            return
        if b"filename" in opts:
            files[name] = MemFile(opts[b"filename"].decode("utf-8", "replace"), bytes(state["data"]))
        else:
            fields[name] = state["data"].decode("utf-8", "replace")

    parser = MultipartParser(params[b"boundary"], {
        "on_part_begin": on_part_begin,
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_part_data": on_part_data,
        "on_part_end": on_part_end,
    })
    try:
        parser.write(body)
        parser.finalize()
    except Exception:  # malformed body; never echo its content
        raise BadForm("malformed multipart body") from None
    return fields, files
