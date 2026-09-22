"""Regression coverage for the async Range-aware file streamer.

The crux (same as issue #3073 / the backup download): under ASGI,
``StreamingHttpResponse`` only *streams* an asynchronous iterator —
a sync one (what ``FileResponse(open(...))`` holds) is drained whole
into a RAM list before the first byte. Previewing a multi-GB video
through the old ``FileResponse`` path ballooned anthias-server's RSS
by the full file size per request and thrashed the device. Every
test here therefore checks ``response.is_async`` and drains via
``aiter(response)`` — exactly what Django's ASGI handler consumes.
"""

import asyncio
from pathlib import Path

import pytest
from django.http import StreamingHttpResponse
from django.test import RequestFactory

from anthias_server.lib.file_stream import BLOCK_SIZE, stream_file_response

CONTENT = b'0123456789abcdef'  # 16 bytes


@pytest.fixture
def factory() -> RequestFactory:
    return RequestFactory()


@pytest.fixture
def sample(tmp_path: Path) -> str:
    path = tmp_path / 'clip.mp4'
    path.write_bytes(CONTENT)
    return str(path)


def _drain(response: StreamingHttpResponse) -> bytes:
    async def go() -> bytes:
        return b''.join([part async for part in aiter(response)])

    return asyncio.run(go())


def _chunks(response: StreamingHttpResponse) -> list[bytes]:
    async def go() -> list[bytes]:
        return [part async for part in aiter(response)]

    return asyncio.run(go())


def test_full_response_is_async_and_streams(
    factory: RequestFactory, sample: str
) -> None:
    response = stream_file_response(factory.get('/x'), sample)
    # The crux of the fix: a sync iterator would leave this False and
    # send Django down the whole-file list()-buffering branch.
    assert response.is_async is True
    assert response.status_code == 200
    assert response['Content-Length'] == str(len(CONTENT))
    assert response['Accept-Ranges'] == 'bytes'
    assert response['Content-Type'] == 'video/mp4'
    assert 'Content-Disposition' not in response
    assert _drain(response) == CONTENT


def test_bounded_range(factory: RequestFactory, sample: str) -> None:
    request = factory.get('/x', HTTP_RANGE='bytes=2-5')
    response = stream_file_response(request, sample)
    assert response.status_code == 206
    assert response['Content-Range'] == f'bytes 2-5/{len(CONTENT)}'
    assert response['Content-Length'] == '4'
    assert _drain(response) == CONTENT[2:6]


def test_open_ended_range(factory: RequestFactory, sample: str) -> None:
    request = factory.get('/x', HTTP_RANGE='bytes=4-')
    response = stream_file_response(request, sample)
    assert response.status_code == 206
    assert _drain(response) == CONTENT[4:]


def test_suffix_range(factory: RequestFactory, sample: str) -> None:
    request = factory.get('/x', HTTP_RANGE='bytes=-4')
    response = stream_file_response(request, sample)
    assert response.status_code == 206
    assert response['Content-Range'] == f'bytes 12-15/{len(CONTENT)}'
    assert _drain(response) == CONTENT[-4:]


def test_range_end_clamped_to_eof(
    factory: RequestFactory, sample: str
) -> None:
    request = factory.get('/x', HTTP_RANGE='bytes=8-999')
    response = stream_file_response(request, sample)
    assert response.status_code == 206
    assert response['Content-Range'] == f'bytes 8-15/{len(CONTENT)}'
    assert _drain(response) == CONTENT[8:]


def test_unsatisfiable_range_is_416(
    factory: RequestFactory, sample: str
) -> None:
    request = factory.get('/x', HTTP_RANGE='bytes=99-')
    response = stream_file_response(request, sample)
    assert response.status_code == 416
    assert response['Content-Range'] == f'bytes */{len(CONTENT)}'
    assert _drain(response) == b''


def test_malformed_range_serves_full_200(
    factory: RequestFactory, sample: str
) -> None:
    request = factory.get('/x', HTTP_RANGE='bytes=abc')
    response = stream_file_response(request, sample)
    assert response.status_code == 200
    assert _drain(response) == CONTENT


def test_multi_range_degrades_to_full_200(
    factory: RequestFactory, sample: str
) -> None:
    request = factory.get('/x', HTTP_RANGE='bytes=0-1,4-5')
    response = stream_file_response(request, sample)
    assert response.status_code == 200
    assert _drain(response) == CONTENT


def test_attachment_disposition(factory: RequestFactory, sample: str) -> None:
    response = stream_file_response(
        factory.get('/x'), sample, as_attachment=True
    )
    assert response['Content-Disposition'] == 'attachment; filename="clip.mp4"'


def test_directory_raises(factory: RequestFactory, tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):
        stream_file_response(factory.get('/x'), str(tmp_path))


def test_missing_file_raises(factory: RequestFactory, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        stream_file_response(factory.get('/x'), str(tmp_path / 'nope.bin'))


def test_large_file_streams_in_bounded_chunks(
    factory: RequestFactory, tmp_path: Path
) -> None:
    """The body arrives as multiple BLOCK_SIZE reads, not one buffered
    blob — the per-request memory bound the fix exists to provide."""
    path = tmp_path / 'big.bin'
    data = bytes(range(256)) * (BLOCK_SIZE // 256 * 2) + b'tail'
    path.write_bytes(data)
    response = stream_file_response(factory.get('/x'), str(path))
    chunks = _chunks(response)
    assert len(chunks) == 3
    assert all(len(chunk) <= BLOCK_SIZE for chunk in chunks)
    assert b''.join(chunks) == data
