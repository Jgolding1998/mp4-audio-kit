import threading
from starlette.testclient import TestClient

from mp4_audio_kit import app as app_module
from mp4_audio_kit.converter import ConvertError, Tools


def test_invalid_upload_is_rejected_before_copying_owned_input(make_client, monkeypatch):
    client = make_client()
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid input must not be copied")
    monkeypatch.setattr(app_module, "_copy_upload", forbidden)
    result = client.post("/api/convert", files={"file": ("bad.mp4", b"not an MP4" * 1000, "video/mp4")})
    assert result.status_code == 415
    assert result.json()["error"] == "not_mp4"
    assert not list(client.app_state.store.root.rglob("input.mp4"))


def test_transient_sweeper_failure_does_not_shutdown_the_app(make_client, monkeypatch):
    sleep = app_module.anyio.sleep
    async def accelerated(seconds):
        await sleep(0.01 if seconds == 60 else seconds)
    monkeypatch.setattr(app_module.anyio, "sleep", accelerated)
    original = app_module.Store.sweep
    recovered = threading.Event()
    calls = [0]
    def sweep(store):
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("Synthetic transient filesystem error")
        original(store)
        recovered.set()
    monkeypatch.setattr(app_module.Store, "sweep", sweep)
    client = make_client()
    assert recovered.wait(2)
    assert client.app_state.store.root.is_dir()
    assert client.get("/").status_code == 200


def test_pending_conversion_reserves_its_result_slot_before_running(make_client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []
    def convert(tools, source, limits, cancel):
        assert source.read_bytes() == head
        calls.append(source)
        entered.set()
        assert release.wait(3)
        output = source.parent / "output.mp3"
        output.write_bytes(b"synthetic audio")
        return output, 1.0
    monkeypatch.setattr(app_module, "convert", convert)
    client = make_client(max_items=1)
    head = b"\0\0\0\x20ftypisom\0\0\0\0"
    def upload():
        return client.post("/api/convert", files={"file": ("tone.mp4", head, "video/mp4")})
    thread = threading.Thread(target=lambda: results.append(upload()), daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        assert upload().status_code == 507
        assert len(calls) == 1
    finally:
        release.set()
        thread.join(5)
    assert len(results) == 1 and results[0].status_code == 200
    assert client.post("/api/clear", json={"ids": [results[0].json()["id"]]}).status_code == 200
    assert upload().status_code == 200
    assert len(calls) == 2


def test_rejected_and_cancelled_uploads_release_their_result_slots(make_client, monkeypatch):
    calls = []
    def convert(tools, source, limits, cancel):
        calls.append(source)
        if len(calls) == 1:
            raise ConvertError("cancelled")
        output = source.parent / "output.mp3"
        output.write_bytes(b"synthetic audio")
        return output, 1.0
    monkeypatch.setattr(app_module, "convert", convert)
    client = make_client(max_items=1)
    def upload(content):
        return client.post("/api/convert", files={"file": ("tone.mp4", content, "video/mp4")})
    assert upload(b"not MP4").status_code == 415
    assert upload(b"\0\0\0\x20ftypisom\0\0\0\0").status_code == 499
    assert upload(b"\0\0\0\x20ftypisom\0\0\0\0").status_code == 200


def test_sweep_errors_do_not_skip_the_owned_runtime_heartbeat(tmp_path, monkeypatch):
    root = tmp_path / "owned-runtime"
    root.mkdir()
    touched = threading.Event()
    sleep = app_module.anyio.sleep
    async def accelerated(seconds):
        await sleep(0.01 if seconds == 60 else seconds)
    def sweep(store):
        raise OSError("Synthetic persistent sweep error")
    monkeypatch.setattr(app_module.anyio, "sleep", accelerated)
    monkeypatch.setattr(app_module.Store, "sweep", sweep)
    monkeypatch.setattr(app_module.runtime, "create", lambda prefix: root)
    monkeypatch.setattr(app_module.runtime, "touch", lambda path: touched.set())
    app = app_module.create_app(port=8766, tools=Tools(("unused",), ("unused",)))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        assert touched.wait(2)
        assert root.exists() and client.get("/").status_code == 200
