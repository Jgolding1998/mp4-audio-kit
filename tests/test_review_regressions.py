import asyncio
import io
import json
import threading
import time

import anyio
import pytest
from starlette.datastructures import UploadFile

from conftest import BASE, PORT, needs_ffmpeg, upload
from test_asgi_edges import call, multipart
from mp4_audio_kit.app import _copy_upload, create_app
from mp4_audio_kit.converter import ConvertError, Item, Limits, Store, Tools, conversion_reservation
from mp4_audio_kit.guard import LocalGuard


def item_in(store, size=5, name="x.mp3"):
    folder = store.new_dir()
    path = folder / "output.mp3"
    path.write_bytes(b"x" * size)
    item = Item(folder.name, folder, path, name, "mp3", size)
    store.add(item)
    return item


def test_zip_overhead_counts_toward_budget(tmp_path):
    store = Store(tmp_path, Limits(max_disk_bytes=10))
    item = item_in(store)
    with pytest.raises(ConvertError) as error:
        store.build_zip([item.id])
    assert error.value.code == "disk_full"
    assert store._reserved == 0 and list(tmp_path.iterdir()) == [item.dir]


def test_zip_folder_creation_failure_releases_reservation_and_slot(tmp_path, monkeypatch):
    store = Store(tmp_path, Limits())
    item = item_in(store)
    real = store.new_dir
    monkeypatch.setattr(store, "new_dir", lambda: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    with pytest.raises(OSError):
        store.build_zip([item.id])
    assert store._reserved == 0 and store._pins == {}
    monkeypatch.setattr(store, "new_dir", real)
    assert store.build_zip([item.id]).path.is_file()


def test_zip_actual_size_stays_within_reserved_budget(tmp_path):
    store = Store(tmp_path, Limits(max_disk_bytes=100_000))
    items = [item_in(store, 1000, "音" * 70 + ".mp3") for _ in range(4)]
    archive = store.build_zip([item.id for item in items])
    assert sum(item.size for item in items) + archive.size <= store.limits.max_disk_bytes
    assert store._reserved == 0 and store._pins == {}


def test_only_one_zip_build_runs_at_a_time(tmp_path, monkeypatch):
    store = Store(tmp_path, Limits())
    item = item_in(store)
    entered, proceed = threading.Event(), threading.Event()
    real = store.new_dir
    result = []

    def delayed():
        entered.set()
        assert proceed.wait(3)
        return real()

    monkeypatch.setattr(store, "new_dir", delayed)
    worker = threading.Thread(target=lambda: result.append(store.build_zip([item.id])))
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(ConvertError) as error:
            store.build_zip([item.id])
        assert error.value.code == "busy"
    finally:
        proceed.set()
        worker.join(3)
    assert len(result) == 1


def test_zip_cancel_checks_within_a_large_member_and_cleans(tmp_path):
    store = Store(tmp_path, Limits())
    item = item_in(store, 3 * 1024 * 1024)
    checks = []

    def cancel():
        checks.append(1)
        if len(checks) == 3:
            store.clear()
            return True
        return False

    with pytest.raises(ConvertError) as error:
        store.build_zip([item.id], cancel)
    assert error.value.code == "cancelled"
    assert checks == [1, 1, 1]
    assert list(tmp_path.iterdir()) == [] and store._reserved == 0 and store._pins == {}


def test_clear_defers_delete_and_budget_release_until_reader_closes(tmp_path):
    store = Store(tmp_path, Limits(max_disk_bytes=100))
    item = item_in(store, 60)
    pinned = store.pin([item.id])
    store.clear()
    assert store.get(item.id) is None and item.path.exists()
    assert not store.reserve(41)
    store.unpin(pinned)
    assert not item.dir.exists() and store.reserve(100)


def test_multiple_readers_keep_one_result_alive(tmp_path):
    store = Store(tmp_path, Limits())
    item = item_in(store)
    first, second = store.pin([item.id]), store.pin([item.id])
    store.drop([item])
    store.unpin(first)
    assert item.path.exists()
    store.unpin(second)
    assert not item.dir.exists()


def test_failed_delete_remains_accounted_and_sweeper_retries(tmp_path, monkeypatch):
    import mp4_audio_kit.converter as module

    store = Store(tmp_path, Limits(max_disk_bytes=100))
    item = item_in(store, 60)
    real = module.shutil.rmtree
    monkeypatch.setattr(module.shutil, "rmtree", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("synthetic open file")))
    store.clear()
    assert item.path.exists() and not store.reserve(41) and store.get(item.id) is None
    monkeypatch.setattr(module.shutil, "rmtree", real)
    store.sweep()
    assert not item.dir.exists() and store.reserve(100)


def test_retained_item_count_is_bounded(tmp_path):
    store = Store(tmp_path, Limits(max_items=2))
    item_in(store)
    item_in(store)
    folder = store.new_dir()
    path = folder / "extra.mp3"
    path.write_bytes(b"x")
    with pytest.raises(ConvertError) as error:
        store.add(Item(folder.name, folder, path, "extra.mp3", "mp3", 1))
    assert error.value.code == "busy" and not store.reserve(1)


def test_conversion_reserves_input_and_encoder_output_headroom():
    limits = Limits(max_file_bytes=1_000_000, max_duration=20)
    assert conversion_reservation(50_000, limits) >= 50_000 + 20 * 24_000 + 64 * 1024


@needs_ffmpeg
def test_upload_refused_before_conversion_when_output_headroom_will_not_fit(make_client, media, monkeypatch):
    import mp4_audio_kit.app as module

    called = []
    monkeypatch.setattr(module, "convert", lambda *args: called.append(True))
    client = make_client(max_disk_bytes=(media / "tone.mp4").stat().st_size + 4096)
    reply = upload(client, media / "tone.mp4")
    assert reply.status_code == 507 and called == [] and client.app_state.store._reserved == 0


def test_copy_checks_initial_small_block_and_cancellation(tmp_path):
    upload_file = UploadFile(io.BytesIO(b"x" * 50), filename="x.mp4")
    with pytest.raises(ConvertError) as error:
        _copy_upload(upload_file, tmp_path / "oversize.mp4", 20)
    assert error.value.code == "too_large" and not (tmp_path / "oversize.mp4").exists()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ConvertError) as error:
        _copy_upload(upload_file, tmp_path / "cancelled.mp4", 100, cancel)
    assert error.value.code == "cancelled" and (tmp_path / "cancelled.mp4").stat().st_size == 0


@pytest.mark.parametrize("length", [b"\xb2", b"-1", b"1.0", b"9" * 16])
def test_non_ascii_or_invalid_lengths_have_fixed_400(length):
    app = LocalGuard(None, port=PORT, token="fixture", body_limit=lambda _: 1024)
    scope = {"type": "http", "method": "POST", "path": "/api/clear", "headers": [
        (b"host", f"127.0.0.1:{PORT}".encode()), (b"origin", BASE.encode()),
        (b"x-csrf-token", b"fixture"), (b"content-length", length)]}
    sent = []

    async def send(message):
        sent.append(message)

    anyio.run(lambda: app(scope, None, send))
    assert sent[0]["status"] == 400 and json.loads(sent[1]["body"])["error"] == "bad_length"


def test_declared_oversize_names_the_per_file_limit_only_for_uploads(tmp_path):
    app = create_app(port=PORT, tools=Tools(("unused",), ("unused",)), root=tmp_path / "rt",
                     limits=Limits(max_file_bytes=1024 * 1024))
    results = {}
    for path, length in (("/api/convert", 1024 * 1024 + 64 * 1024 + 1), ("/api/clear", 16 * 1024 + 1)):
        scope = {"type": "http", "method": "POST", "path": path, "headers": [
            (b"host", f"127.0.0.1:{PORT}".encode()), (b"origin", BASE.encode()),
            (b"x-csrf-token", app.state.token.encode()), (b"content-length", str(length).encode())]}
        sent = []

        async def send(message):
            sent.append(message)

        anyio.run(lambda: app(scope, None, send))
        assert sent[0]["status"] == 413
        results[path] = json.loads(sent[1]["body"])
    assert results["/api/convert"]["error"] == "too_large"
    assert "1 MB per-file limit" in results["/api/convert"]["message"] and "--max-mb" in results["/api/convert"]["message"]
    assert str(tmp_path) not in results["/api/convert"]["message"]
    assert results["/api/clear"]["message"] == "The request is too large."


def test_server_task_cancellation_signals_worker_and_cleans(tmp_path, monkeypatch):
    import mp4_audio_kit.app as module

    app = create_app(port=PORT, tools=Tools(("unused",), ("unused",)), root=tmp_path / "rt")
    started = threading.Event()
    observed = []

    def slow(tools, src, limits, cancel):
        started.set()
        observed.append(cancel.wait(2))
        raise ConvertError("cancelled")

    async def exercise():
        body = multipart(b"\x00\x00\x00\x20ftypisom" + b"\0" * 100)
        task = asyncio.create_task(call(app, [body], length=len(body)))
        while not started.is_set():
            await asyncio.sleep(0.01)
        before = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - before < 1

    monkeypatch.setattr(module, "convert", slow)
    asyncio.run(exercise())
    assert observed == [True] and list((tmp_path / "rt").iterdir()) == []
    assert app.state.store._reserved == 0


def test_clear_cancels_inflight_conversion_before_it_can_publish(make_client, monkeypatch):
    import mp4_audio_kit.app as module

    client = make_client()
    started, finished = threading.Event(), threading.Event()
    replies = []

    def slow(tools, src, limits, cancel):
        started.set()
        try:
            assert cancel.wait(3)
            raise ConvertError("cancelled")
        finally:
            finished.set()

    monkeypatch.setattr(module, "convert", slow)
    data = b"\x00\x00\x00\x20ftypisom" + b"\0" * 100
    worker = threading.Thread(target=lambda: replies.append(client.post("/api/convert", files={"file": ("x.mp4", data, "video/mp4")})))
    worker.start()
    assert started.wait(2)
    assert client.post("/api/clear", json={}).status_code == 200
    assert finished.wait(2)
    worker.join(2)
    assert len(replies) == 1 and replies[0].status_code in (422, 499)
    assert list(client.app_state.store.root.iterdir()) == []
    assert client.app_state.store._reserved == 0


def test_zip_filesystem_failure_is_fixed_public_error(make_client, monkeypatch):
    client = make_client()
    store = client.app_state.store
    item = item_in(store)
    monkeypatch.setattr(store, "new_dir", lambda: (_ for _ in ()).throw(OSError("PLANTED_PRIVATE_PATH")))
    reply = client.post("/api/zip", json={"ids": [item.id]})
    assert reply.status_code == 422 and reply.json()["error"] == "failed"
    assert "PLANTED_PRIVATE" not in reply.text and store._reserved == 0 and store._pins == {}


def download_scope(item_id):
    path = "/api/download/" + item_id
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
            "headers": [(b"host", f"127.0.0.1:{PORT}".encode())],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT)}


async def no_disconnect():
    await anyio.sleep_forever()


def test_clear_during_download_finishes_existing_reader_then_removes_file(tmp_path):
    app = create_app(port=PORT, tools=Tools(("unused",), ("unused",)), root=tmp_path / "rt", limits=Limits(max_disk_bytes=100))
    store = app.state.store
    item = item_in(store, 60)
    bodies = []

    async def exercise():
        entered, proceed = anyio.Event(), anyio.Event()

        async def send(message):
            if message["type"] == "http.response.start":
                entered.set()
                await proceed.wait()
            elif message["type"] == "http.response.body":
                bodies.append(message.get("body", b""))

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(app, download_scope(item.id), no_disconnect, send)
            await entered.wait()
            store.clear()
            assert item.path.exists() and store.get(item.id) is None and not store.reserve(41)
            proceed.set()

    anyio.run(exercise)
    assert b"".join(bodies) == b"x" * 60
    assert store._pins == {} and not item.dir.exists() and store.reserve(100)


def test_cancelled_download_releases_pin_even_in_cancelled_scope(tmp_path):
    app = create_app(port=PORT, tools=Tools(("unused",), ("unused",)), root=tmp_path / "rt")
    store = app.state.store
    item = item_in(store)

    async def exercise():
        entered = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.start":
                entered.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(app(download_scope(item.id), no_disconnect, send))
        await entered.wait()
        store.clear()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert store._pins == {} and not item.dir.exists()


def test_new_zip_retires_older_zip_from_budget(tmp_path):
    # Two MP3s of 1000 bytes; the budget fits the MP3s plus ONE zip, not two.
    store = Store(tmp_path / "rt", Limits(max_disk_bytes=2000 + 8000))
    ids = [item_in(store, 1000, f"{n}.mp3").id for n in "ab"]
    first = store.build_zip(ids)
    second = store.build_zip(ids)  # would be disk_full if the first ZIP stayed charged
    assert store.get(first.id) is None and not first.dir.exists()
    assert store.get(second.id) is not None and second.path.is_file()
    assert all(store.get(i) is not None for i in ids)  # the MP3s themselves are untouched


def test_zip_being_downloaded_survives_until_reader_closes(tmp_path):
    store = Store(tmp_path / "rt", Limits())
    ids = [item_in(store, 100, f"{n}.mp3").id for n in "ab"]
    first = store.build_zip(ids)
    reader = store.pin([first.id])
    store.build_zip(ids)
    assert store.get(first.id) is None and first.path.is_file()  # hidden from new requests, still readable
    with store._lock:
        assert first.id in store._items  # and still charged
    store.unpin(reader)
    assert not first.dir.exists()
