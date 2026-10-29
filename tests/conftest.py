"""Shared fixtures. All media is synthetic (sine tones and solid colour video made by ffmpeg)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mp4_audio_kit.app import create_app
from mp4_audio_kit.converter import Limits, find_tools

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
FAKES = Path(__file__).parent / "fakes"
TOOLS = find_tools()
needs_ffmpeg = pytest.mark.skipif(TOOLS is None, reason="ffmpeg/ffprobe not on PATH")

if TOOLS is None and os.environ.get("MAK_REQUIRE_FFMPEG") == "1":
    raise RuntimeError("MAK_REQUIRE_FFMPEG=1 but ffmpeg/ffprobe were not found")


def _ffmpeg(*args):
    subprocess.run([TOOLS.ffmpeg[0], "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                   check=True, timeout=60)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    if TOOLS is None:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    d = tmp_path_factory.mktemp("media")
    video = ["-f", "lavfi", "-i", "color=c=0x3366cc:s=160x120:r=10"]
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3", *video,
            "-ac", "2", "-shortest", "-c:v", "mpeg4", "-c:a", "aac",
            "-metadata", "title=SECRET-TITLE-123", "-metadata", "artist=SECRET-ARTIST", str(d / "tone.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=2", *video,
            "-ac", "6", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(d / "surround.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=2", "-c:v", "mpeg4", str(d / "silent.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libvorbis", str(d / "audio.webm"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame", str(d / "audio.mp3"))
    # Audio-only AAC at 44.1 kHz: track timescale is 1/44100, so setts offsets below are in samples.
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6", "-ac", "1",
            "-c:a", "aac", str(d / "plain.mp4"))
    _ffmpeg("-i", str(d / "plain.mp4"), "-c", "copy", "-bsf:a", "setts=ts='if(gt(N,100),PTS+176400,PTS)'",
            str(d / "gap.mp4"))
    _ffmpeg("-i", str(d / "plain.mp4"), "-c", "copy", "-bsf:a", "setts=ts='if(gt(N,100),PTS+1270080000,PTS)'",
            str(d / "jump.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=50", "-ac", "1",
            "-c:a", "aac", "-b:a", "24k", str(d / "lowrate.mp4"))
    data = (d / "tone.mp4").read_bytes()
    (d / "truncated.mp4").write_bytes(data[: len(data) // 3])
    (d / "notes.mp4").write_bytes(b"just some text, not a video\n" * 10)
    return d


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(tools=None, **overrides):
        limits = Limits(**{"timeout": 30, **overrides})
        app = create_app(port=PORT, tools=tools or TOOLS, limits=limits, root=tmp_path / "runtime")
        client = TestClient(app, base_url=BASE)
        client.__enter__()
        client.headers.update({"Origin": BASE, "X-CSRF-Token": app.state.token})
        client.app_state = app.state
        client.raw_app = app
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def slow_tools():
    # Base interpreter: a venv python.exe launcher would be a second process, and ffmpeg jobs allow one.
    python = getattr(sys, "_base_executable", sys.executable)
    return TOOLS.__class__((python, "-I", str(FAKES / "slow_tool.py")), TOOLS.ffprobe)


def upload(client, path, name=None, ctype="video/mp4"):
    with open(path, "rb") as f:
        return client.post("/api/convert", files={"file": (name or Path(path).name, f, ctype)})


def runtime_dirs(client):
    root = client.app_state.store.root
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def ffprobe_json(path):
    import json

    out = subprocess.run([TOOLS.ffprobe[0], "-v", "error", "-show_entries",
                          "format=duration,format_name:stream=codec_name,channels,sample_rate:format_tags",
                          "-of", "json", str(path)], capture_output=True, check=True, timeout=30)
    return json.loads(out.stdout)


def decode_pcm(path):
    """Decode to mono 16-bit PCM at 44.1 kHz for content checks."""
    out = subprocess.run([TOOLS.ffmpeg[0], "-v", "error", "-nostdin", "-i", str(path), "-ac", "1", "-ar", "44100",
                          "-f", "s16le", "-"], capture_output=True, check=True, timeout=30)
    return out.stdout

