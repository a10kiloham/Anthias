"""Async, Range-aware local-file streaming for the ASGI stack.

Django's ``FileResponse`` wraps a *synchronous* file iterator, and
under ASGI ``StreamingHttpResponse.__aiter__`` consumes any sync
iterator via ``await sync_to_async(list)(...)`` — the ENTIRE file is
buffered into a RAM list before the first response byte goes out.
That is the same mechanism as issue #3073 (the backup archive), but
on the asset endpoints the payload is an operator's video: previewing
a multi-GB clip ballooned anthias-server's RSS by the whole file per
request and thrashed the device into unresponsiveness. Every view
that serves a local file must therefore hand ``StreamingHttpResponse``
an *async* iterator; this module is the shared implementation.

Range support belongs at the same layer: ``<video>`` preview/scrub
issues ``Range:`` requests, and answering each one with the whole
file (FileResponse has no range handling either) multiplies the
cost. A single ``bytes=start-end`` range is honoured with 206;
malformed or multi-range headers safely degrade to the full 200.
"""

import io
import mimetypes
import os
import re
from collections.abc import AsyncGenerator

from asgiref.sync import sync_to_async
from django.http import HttpRequest, StreamingHttpResponse

# 256 KiB per read: large enough to keep throughput off the
# per-chunk executor-hop overhead, small enough that per-request
# memory stays flat.
BLOCK_SIZE = 256 * 1024

_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')


async def _aread_file(
    path: str, start: int, length: int
) -> AsyncGenerator[bytes]:
    """Yield ``length`` bytes of ``path`` from offset ``start`` in
    BLOCK_SIZE chunks, doing every blocking touch of the file off the
    event loop. Closes the handle on exhaustion AND on client
    disconnect (Django aclose()s the generator, which runs finally).
    """

    def _open() -> io.BufferedReader:
        return open(path, 'rb')

    handle = await sync_to_async(_open, thread_sensitive=False)()
    try:
        if start:
            await sync_to_async(handle.seek, thread_sensitive=False)(start)
        remaining = length
        while remaining > 0:
            chunk = await sync_to_async(handle.read, thread_sensitive=False)(
                min(BLOCK_SIZE, remaining)
            )
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        await sync_to_async(handle.close, thread_sensitive=False)()


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Return (start, end) inclusive for a single satisfiable byte
    range, or None for anything else (absent handling → full 200;
    the caller separately answers 416 for a syntactically valid but
    unsatisfiable range)."""
    match = _RANGE_RE.match(header)
    if match is None or size == 0:
        return None
    start_s, end_s = match.groups()
    if start_s:
        start = int(start_s)
        if start >= size:
            return None
        end = min(int(end_s), size - 1) if end_s else size - 1
        if end < start:
            return None
        return start, end
    if end_s:
        # bytes=-N — the final N bytes.
        suffix = int(end_s)
        if suffix == 0:
            return None
        return max(size - suffix, 0), size - 1
    return None


def stream_file_response(
    request: HttpRequest,
    path: str,
    *,
    content_type: str | None = None,
    as_attachment: bool = False,
) -> StreamingHttpResponse:
    """Async-streaming replacement for ``FileResponse(open(path))``.

    Raises the same ``FileNotFoundError`` / ``IsADirectoryError`` as
    ``open()`` would (via the stat), so existing except blocks keep
    working.
    """
    if os.path.isdir(path):
        raise IsADirectoryError(path)
    size = os.path.getsize(path)

    if content_type is None:
        content_type = (
            mimetypes.guess_type(path)[0] or 'application/octet-stream'
        )

    range_header = request.headers.get('Range', '')
    byte_range = _parse_range(range_header, size) if range_header else None

    if byte_range is None and _RANGE_RE.match(range_header or ''):
        # Well-formed but unsatisfiable (start past EOF, empty file,
        # inverted bounds): RFC 9110 wants 416 + the current length.
        response = StreamingHttpResponse(
            _aread_file(path, 0, 0), status=416, content_type=content_type
        )
        response['Content-Range'] = f'bytes */{size}'
        response['Accept-Ranges'] = 'bytes'
        return response

    if byte_range is not None:
        start, end = byte_range
        length = end - start + 1
        response = StreamingHttpResponse(
            _aread_file(path, start, length),
            status=206,
            content_type=content_type,
        )
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
    else:
        length = size
        response = StreamingHttpResponse(
            _aread_file(path, 0, size), content_type=content_type
        )

    response['Content-Length'] = str(length)
    response['Accept-Ranges'] = 'bytes'
    if as_attachment:
        basename = os.path.basename(path).replace('"', '')
        response['Content-Disposition'] = f'attachment; filename="{basename}"'
    return response
