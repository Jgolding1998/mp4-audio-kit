"""Unit checks and real FFmpeg conversions of synthetic MP4 files."""

import math
import shutil
import struct
import threading
import time
import zipfile
from pathlib import Path

import pytest

from conftest import TOOLS, decode_pcm, ffprobe_json, needs_ffmpeg, slow_tools
from mp4_audio_kit.converter import (
    ConvertError, Item, Limits, Store, check_upload, convert, convert_argv, display_name, probe_argv, unique_names,
)

GOOD_HEAD = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00"


@pytest.mark.parametrize("name, ctype, head", [
    ("a.mp4", "video/mp4", GOOD_HEAD),
    ("A.M4V", "video/x-m4v", GOOD_HEAD),
    ("a.mp4", "application/octet-stream", GOOD_HEAD),
])
def test_check_upload_accepts(name, ctype, head):
    check_upload(name, ctype, head)


@pytest.mark.parametrize("name, ctype, head", [
    ("a.mov", "video/mp4", GOOD_HEAD),
    ("a.mp4.exe", "video/mp4", GOOD_HEAD),
    ("", "video/mp4", GOOD_HEAD),
    ("a.mp4", "text/plain", GOOD_HEAD),
    ("a.mp4", "video/webm", GOOD_HEAD),
    ("a.mp4", "", GOOD_HEAD),
    ("a.mp4", "video/mp4", b"hello world, this is text"),
    ("a.mp4", "video/mp4", b"\x1aE\xdf\xa3" + b"\x00" * 12),        # Matroska/WebM
    ("a.mp4", "video/mp4", b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00"),  # MP3
    ("a.mp4", "video/mp4", b"\x00\x00\x00\x00ftypisom\x00\x00"),       # box size 0
    ("a.mp4", "video/mp4", b"\xff\xff\xff\xffftypisom\x00\x00"),       # absurd box size
    ("a.mp4", "video/mp4", b"\x00\x00\x00\x20ftyp\x00\x01\x02\x03"),   # non-text brand
    ("a.mp4", "video/mp4", b"\x00\x00"),
])
def test_check_upload_rejects(name, ctype, head):
    with pytest.raises(ConvertError) as err:
        check_upload(name, ctype, head)
    assert err.value.code == "not_mp4"


@pytest.mark.parametrize("raw, expected", [
    ("holiday.mp4", "holiday.mp3"),
    ("../../../etc/evil.mp4", "evil.mp3"),
    ("Z:\\synthetic-fixtures\\clip.MP4", "clip.mp3"),
    ("-i http:evil.mp4", "i http_evil.mp3"),
    ("-f lavfi -i sine.mp4", "f lavfi -i sine.mp3"),
    ("CON.mp4", "_CON.mp3"),
    (".mp4", "audio.mp3"),
    (None, "audio.mp3"),
    ("a\u202egnp.mp4", "agnp.mp3"),
])
def test_display_name(raw, expected):
    assert display_name(raw) == expected


def test_argv_is_fixed_and_locked_down():
    tools = TOOLS or type("T", (), {"ffmpeg": ("ffmpeg",), "ffprobe": ("ffprobe",)})()
    src, dst = Path("job/input.mp4"), Path("job/output.part.mp3")
    for argv in (convert_argv(tools, src, dst, Limits(), 2), probe_argv(tools, src)):
        i = argv.index("-i")
        before = argv[:i]
        for opt, val in (("-f", "mov"), ("-enable_drefs", "0"), ("-use_absolute_path", "0"),
                         ("-protocol_whitelist", "file")):
            assert before[before.index(opt) + 1] == val
        assert argv[i + 1] == f"file:{src}"
    argv = convert_argv(tools, src, dst, Limits(), 6)
    assert "-nostdin" in argv and argv[-2:] == ["-n", f"file:{dst}"]
    for opt, val in (("-map_metadata", "-1"), ("-map_chapters", "-1"), ("-c:a", "libmp3lame"), ("-b:a", "192k"),
                     ("-id3v2_version", "0"), ("-write_id3v1", "0"), ("-ac", "2"), ("-map", "0:a:0")):
        assert argv[argv.index(opt) + 1] == val
    assert "-ac" not in convert_argv(tools, src, dst, Limits(), 2)


def test_unique_names():
    assert unique_names(["a.mp3", "A.mp3", "a.mp3", "a (2).mp3", "b.mp3"]) == [
        "a.mp3", "A (2).mp3", "a (3).mp3", "a (2) (2).mp3", "b.mp3"]


def _job(tmp_path, src):
    job = tmp_path / "job"
    job.mkdir()
    shutil.copy(src, job / "input.mp4")
    return job / "input.mp4"


def _frequency(pcm, rate=44100):
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    body = samples[rate // 10: -rate // 10]  # skip encoder padding at both ends
    crossings = sum(1 for a, b in zip(body, body[1:]) if a < 0 <= b)
    rms = math.sqrt(sum(s * s for s in body) / len(body))
    return crossings / (len(body) / rate), rms


@needs_ffmpeg
def test_real_conversion_content(media, tmp_path):
    out, seconds = convert(TOOLS, _job(tmp_path, media / "tone.mp4"), Limits(), threading.Event())
    assert out.name == "output.mp3" and sorted(p.name for p in out.parent.iterdir()) == ["input.mp4", "output.mp3"]
    info = ffprobe_json(out)
    assert [s["codec_name"] for s in info["streams"]] == ["mp3"]
    assert info["streams"][0]["channels"] == 2
    assert abs(float(info["format"]["duration"]) - 3.0) < 0.15 and abs(seconds - 3.0) < 0.15
    assert not info["format"].get("tags")
    data = out.read_bytes()
    assert 0.85 * 72000 < len(data) < 1.15 * 72000          # 192 kbit/s for 3 s
    assert not data.startswith(b"ID3") and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
    assert b"SECRET" not in data and b"TAG" != data[-128:-125]
    freq, rms = _frequency(decode_pcm(out))
    assert abs(freq - 440) < 5 and rms > 1000


@needs_ffmpeg
def test_surround_is_downmixed(media, tmp_path):
    out, seconds = convert(TOOLS, _job(tmp_path, media / "surround.mp4"), Limits(), threading.Event())
    info = ffprobe_json(out)
    assert info["streams"][0]["channels"] == 2 and abs(seconds - 2.0) < 0.15
    freq, _ = _frequency(decode_pcm(out))
    assert abs(freq - 1000) < 10


@needs_ffmpeg
@pytest.mark.parametrize("name, code, limits", [
    ("silent.mp4", "no_audio", Limits()),
    ("truncated.mp4", "unreadable", Limits()),
    ("notes.mp4", "unreadable", Limits()),
    ("audio.webm", "unreadable", Limits()),
    ("audio.mp3", "unreadable", Limits()),
    ("tone.mp4", "too_long", Limits(max_duration=1)),
])
def test_conversion_rejections(media, tmp_path, name, code, limits):
    src = _job(tmp_path, media / name)
    with pytest.raises(ConvertError) as err:
        convert(TOOLS, src, limits, threading.Event())
    assert err.value.code == code
    assert not list(src.parent.glob("output*"))


@needs_ffmpeg
def test_conversion_timeout_and_cancel(media, tmp_path):
    src = _job(tmp_path, media / "tone.mp4")
    start = time.monotonic()
    with pytest.raises(ConvertError) as err:
        convert(slow_tools(), src, Limits(timeout=1), threading.Event())
    assert err.value.code == "timeout" and time.monotonic() - start < 15
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(ConvertError) as err:
        convert(slow_tools(), src, Limits(), cancel)
    assert err.value.code == "cancelled"
    assert not list(src.parent.glob("output*"))


@needs_ffmpeg
def test_zip_entries(media, tmp_path):
    store = Store(tmp_path / "rt", Limits())
    ids = []
    for name in ("tone.mp3", "tone.mp3", "TONE.mp3", "x.mp3"):
        d = store.new_dir()
        (d / "output.mp3").write_bytes(name.encode() * 10)
        item = Item(d.name, d, d / "output.mp3", name, "mp3", 80)
        store.add(item)
        ids.append(item.id)
    z = store.build_zip(ids)
    with zipfile.ZipFile(z.path) as zf:
        assert zf.namelist() == ["tone.mp3", "tone (2).mp3", "TONE (3).mp3", "x.mp3"]
        assert zf.testzip() is None
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
    assert z.name == "mp3-files.zip" and not list(z.dir.glob("*.part.zip"))
    with pytest.raises(ConvertError):
        store.build_zip([ids[0], z.id])      # a ZIP cannot contain a ZIP
    with pytest.raises(ConvertError):
        store.build_zip([ids[0], "0" * 32])
    store.shutdown()
    assert not (tmp_path / "rt").exists()


def test_store_budget_and_ttl(tmp_path):
    store = Store(tmp_path / "rt", Limits(max_disk_bytes=100, ttl=0))
    assert store.reserve(60) and not store.reserve(60)
    store.release(60)
    assert store.reserve(100)
    store.release(100)
    d = store.new_dir()
    store.add(Item(d.name, d, d / "o.mp3", "o.mp3", "mp3", 10))
    time.sleep(0.01)
    store.sweep()
    assert store.get(d.name) is None and not d.exists()
