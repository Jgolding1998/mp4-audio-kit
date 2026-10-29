"""Raw ASGI tests for things a normal HTTP client will not do: disconnects and lying lengths."""

import time

import anyio
import pytest

from conftest import BASE, PORT, TOOLS, needs_ffmpeg, slow_tools
from mp4_audio_kit.app import create_app
from mp4_audio_kit.converter import Limits

pytestmark = needs_ffmpeg
BOUNDARY = "testboundary123"


def multipart(data: bytes, name="tone.mp4") -> bytes:
    head = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            "Content-Type: video/mp4\r\n\r\n").encode()
    return head + data + f"\r\n--{BOUNDARY}--\r\n".encode()


async def call(app, chunks, *, length, disconnect_after=None, complete=True):
    """Send body chunks; once they run out, report a disconnect after the given delay."""
    token = app.state.token
    headers = [(b"host", f"127.0.0.1:{PORT}".encode()), (b"origin", BASE.encode()),
               (b"x-csrf-token", token.encode()), (b"content-length", str(length).encode()),
               (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/api/convert", "raw_path": b"/api/convert", "root_path": "",
             "query_string": b"", "headers": headers, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT)}
    pending = list(chunks)
    started = []
    sent = []

    async def receive():
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending) or not complete}
        if disconnect_after is None:
            await anyio.sleep_forever()
        if not started:
            started.append(time.monotonic())
        if time.monotonic() - started[0] >= disconnect_after:
            return {"type": "http.disconnect"}
        await anyio.sleep_forever()

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def entries(root):
    return list(root.iterdir()) if root.exists() else []


def test_disconnect_during_upload_cleans_up(tmp_path, media):
    app = create_app(port=PORT, tools=TOOLS, root=tmp_path / "rt")
    body = multipart((media / "tone.mp4").read_bytes())
    status, _ = anyio.run(lambda: call(app, [body[:5000]], length=len(body), disconnect_after=0, complete=False))
    assert status == 499
    assert entries(tmp_path / "rt") == [] and app.state.store.reserve(Limits().max_disk_bytes)


def test_disconnect_during_conversion_kills_and_cleans(tmp_path, media):
    app = create_app(port=PORT, tools=slow_tools(), limits=Limits(timeout=60), root=tmp_path / "rt")
    body = multipart((media / "tone.mp4").read_bytes())
    start = time.monotonic()
    status, _ = anyio.run(lambda: call(app, [body], length=len(body), disconnect_after=1.0))
    assert status == 499 and time.monotonic() - start < 15
    assert entries(tmp_path / "rt") == []


@pytest.mark.parametrize("declared_delta", [-100, -1])
def test_body_longer_than_declared_is_rejected(tmp_path, media, declared_delta):
    app = create_app(port=PORT, tools=TOOLS, root=tmp_path / "rt")
    body = multipart((media / "tone.mp4").read_bytes())
    status, payload = anyio.run(lambda: call(app, [body[:1000], body[1000:]], length=len(body) + declared_delta))
    assert status == 413 and b"too_large" in payload
    assert entries(tmp_path / "rt") == []


def test_upload_over_limit_mid_stream(tmp_path, media):
    data = (media / "tone.mp4").read_bytes()
    limits = Limits(max_file_bytes=len(data) - 10)
    app = create_app(port=PORT, tools=TOOLS, limits=limits, root=tmp_path / "rt")
    body = multipart(data)
    status, _ = anyio.run(lambda: call(app, [body], length=len(body)))
    assert status == 413 and entries(tmp_path / "rt") == []
