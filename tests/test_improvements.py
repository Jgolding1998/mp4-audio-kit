"""Regression tests for flaws found in the security review and docs comparison."""

import os
import time
from pathlib import Path

from conftest import needs_ffmpeg, runtime_dirs, upload
from mp4_audio_kit import runtime
from mp4_audio_kit.converter import Limits


def test_deeply_nested_json_is_a_clean_400(make_client):
    client = make_client()
    body = '{"ids": ' + "[" * 1500 + "]" * 1500 + "}"
    r = client.post("/api/zip", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400


@needs_ffmpeg
def test_failed_folder_creation_does_not_leak_slots(make_client, media, monkeypatch):
    client = make_client(max_uploads=1)
    store = client.app_state.store
    real = store.new_dir

    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(store, "new_dir", broken)
    assert upload(client, media / "tone.mp4").status_code == 422
    monkeypatch.setattr(store, "new_dir", real)
    assert upload(client, media / "tone.mp4").status_code == 200  # slot was released
    assert store.reserve(Limits().max_disk_bytes - store.get(runtime_dirs(client)[0]).size)


@needs_ffmpeg
def test_busy_when_upload_slots_are_full(make_client, media):
    client = make_client(max_uploads=0)
    r = upload(client, media / "tone.mp4")
    assert r.status_code == 503 and r.json()["error"] == "busy" and runtime_dirs(client) == []


def test_stale_runtime_folders_are_removed(tmp_path):
    prefix = "mp4-audio-kit-"
    stale, fresh, foreign = tmp_path / f"{prefix}old", tmp_path / f"{prefix}new", tmp_path / f"{prefix}x"
    for d in (stale, fresh, foreign):
        d.mkdir()
    runtime.touch(stale)
    runtime.touch(fresh)
    old = time.time() - 2 * runtime.STALE_SECONDS
    os.utime(stale / runtime.MARKER, (old, old))
    assert runtime.remove_stale(prefix, base=tmp_path) == 1
    assert not stale.exists() and fresh.exists() and foreign.exists()


def test_requirements_pin_matches_pyproject():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    req = (root / "requirements.txt").read_text()
    for dep in deps:
        assert f"{dep} \\" in req, dep
    assert all("--hash=sha256:" in block for block in req.split("==")[1:])
