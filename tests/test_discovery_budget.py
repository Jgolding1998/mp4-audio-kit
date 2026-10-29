import os
from pathlib import Path

from mp4_audio_kit.converter import Limits, conversion_reservation, find_tools, output_bound


def test_tool_discovery_ignores_current_directory_and_relative_path_entries(tmp_path, monkeypatch):
    cwd, trusted = tmp_path / "working", tmp_path / "trusted bin"
    cwd.mkdir(); trusted.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    for directory in (cwd, trusted):
        for name in ("ffmpeg", "ffprobe"):
            tool = directory / (name + suffix)
            tool.write_bytes(b"OWNED NONEXECUTED DISCOVERY FIXTURE")
            tool.chmod(0o700)
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(cwd), str(trusted)]))
    tools = find_tools()
    assert tools.ffmpeg == (str((trusted / ("ffmpeg" + suffix)).resolve()),)
    assert tools.ffprobe == (str((trusted / ("ffprobe" + suffix)).resolve()),)


def test_tool_discovery_skips_a_denied_path_entry(tmp_path, monkeypatch):
    denied, trusted = tmp_path / "denied", tmp_path / "trusted"
    denied.mkdir(); trusted.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("ffmpeg", "ffprobe"):
        tool = trusted / (name + suffix)
        tool.write_bytes(b"OWNED NONEXECUTED DISCOVERY FIXTURE"); tool.chmod(0o700)
    original = Path.is_file
    def is_file(path):
        if path.parent == denied:
            raise PermissionError("Owned denied-entry fixture")
        return original(path)
    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setenv("PATH", os.pathsep.join([str(denied), str(trusted)]))
    tools = find_tools()
    assert tools.ffmpeg == (str((trusted / ("ffmpeg" + suffix)).resolve()),)
    assert tools.ffprobe == (str((trusted / ("ffprobe" + suffix)).resolve()),)


def test_conversion_budget_counts_spooled_upload_and_owned_input_copy():
    limits = Limits(max_duration=20)
    length = 2_000_000
    assert conversion_reservation(length, limits) >= 2 * length + output_bound(limits) + 64 * 1024


def test_spool_overlap_is_reserved_and_released_before_conversion(make_client, monkeypatch):
    from mp4_audio_kit import app as module
    payload = b"\x00\x00\x00\x18ftypisom" + b"\0" * (1100 * 1024)
    client = make_client(max_duration=20)
    store = client.app_state.store
    original = module._copy_upload
    stages = []

    def copy(upload, dst, limit, cancel):
        assert upload.file._rolled, "exercise the actual disk-backed multipart spool"
        assert store._reserved >= 2 * len(payload) + output_bound(Limits(max_duration=20))
        stages.append("spooled_and_copy_reserved")
        return original(upload, dst, limit, cancel)

    def convert(tools, src, limits, cancel):
        assert store._reserved >= len(payload) + output_bound(limits)
        assert store._reserved < 2 * len(payload) + output_bound(limits)
        stages.append("spool_headroom_released")
        path = src.with_name("owned-output.mp3")
        path.write_bytes(b"OWNED RESOURCE TEST OUTPUT")
        return path, 1

    monkeypatch.setattr(module, "_copy_upload", copy)
    monkeypatch.setattr(module, "convert", convert)
    reply = client.post("/api/convert", files={"file": ("owned.mp4", payload, "video/mp4")})
    assert reply.status_code == 200
    assert stages == ["spooled_and_copy_reserved", "spool_headroom_released"]
    assert store._reserved == 0


def test_upload_is_refused_before_copy_if_two_inputs_do_not_fit(make_client, monkeypatch):
    from mp4_audio_kit import app as module
    payload = b"\x00\x00\x00\x18ftypisom" + b"\0" * 100_000
    limits = Limits(max_duration=1)
    client = make_client(max_duration=1, max_disk_bytes=len(payload) + output_bound(limits) + 70_000)
    copied = []
    monkeypatch.setattr(module, "_copy_upload", lambda *args: copied.append(True))
    reply = client.post("/api/convert", files={"file": ("owned.mp4", payload, "video/mp4")})
    assert reply.status_code == 507 and copied == []
    assert client.app_state.store._reserved == 0
