"""MP4 to MP3 with ffmpeg: fixed argv, forced MOV/MP4 demuxer, file protocol only."""

from __future__ import annotations

import json
import math
import os
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .names import safe_stem
from .procguard import run_bounded

BITRATE = "192k"
BYTES_PER_SECOND = 192_000 // 8
UPLOAD_EXTS = (".mp4", ".m4v")
UPLOAD_TYPES = {"video/mp4", "video/x-m4v", "application/octet-stream"}
MAX_ALLOC = str(512 * 1024 * 1024)
# Input options: MOV/MP4 demuxer only, no external data references, local files only.
_INPUT_OPTS = ["-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0"]


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 2 * 1024**3
    max_duration: int = 6 * 3600
    max_disk_bytes: int = 8 * 1024**3
    timeout: float = 20 * 60
    probe_timeout: float = 30
    max_concurrent: int = 2
    max_uploads: int = 4
    max_zip_items: int = 50
    ttl: float = 30 * 60
    memory_bytes: int = 1024**3
    max_items: int = 200


@dataclass(frozen=True)
class Tools:
    ffmpeg: tuple
    ffprobe: tuple


def find_tools() -> Optional[Tools]:
    def which(name: str) -> Optional[str]:
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
        for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
            if not entry or not os.path.isabs(entry):
                continue
            directory = os.path.abspath(entry)
            if os.path.normcase(directory) == cwd:
                continue
            candidate = Path(directory) / (name + ".exe" if os.name == "nt" else name)
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate.resolve())
            except OSError:
                continue
        return None

    ffmpeg, ffprobe = which("ffmpeg"), which("ffprobe")
    if not ffmpeg or not ffprobe:
        return None
    return Tools((str(Path(ffmpeg).resolve()),), (str(Path(ffprobe).resolve()),))


def tools_ready(tools: Tools) -> bool:
    """Check the installed executables under the same one-tool process limit."""
    try:
        with tempfile.TemporaryDirectory(prefix="mp4-audio-kit-tools-") as folder:
            work = Path(folder)
            for command, name in ((tools.ffprobe, b"ffprobe version "), (tools.ffmpeg, b"ffmpeg version ")):
                result = run_bounded([*command, "-version"], cwd=folder, env=child_env(work),
                                     timeout=5, memory_bytes=Limits().memory_bytes, max_processes=1)
                if result.stopped or result.returncode != 0 or not result.stdout.startswith(name):
                    return False
        return True
    except Exception:
        return False


class ConvertError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


MESSAGES = {
    "not_mp4": "This is not an MP4 video file.",
    "bad_upload": "The upload was not understood. Choose the file again.",
    "too_large": "This file is larger than the app's size limit.",
    "too_long": "This video is longer than the app's length limit.",
    "no_audio": "This video has no audio track.",
    "unreadable": "This MP4 file could not be read. It may be damaged.",
    "timeout": "The conversion took too long and was stopped.",
    "cancelled": "The conversion was cancelled.",
    "failed": "The conversion failed.",
    "busy": "The app is busy. Try again in a moment.",
    "disk_full": "The app's temporary space or result limit is full. Download or clear finished files first.",
    "not_found": "This file is not available anymore.",
    "bad_request": "Bad request.",
}


def child_env(work_dir: Path) -> dict:
    env = {"PATH": os.defpath, "TEMP": str(work_dir), "TMP": str(work_dir), "TMPDIR": str(work_dir),
           "HOME": str(work_dir), "USERPROFILE": str(work_dir)}
    for key in ("SYSTEMROOT", "WINDIR"):
        if os.name == "nt" and os.environ.get(key):
            env[key] = os.environ[key]
    return env


def display_name(filename: Optional[str]) -> str:
    """Untrusted upload name to a safe .mp3 download name. Never used as a path."""
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if base.lower().endswith(UPLOAD_EXTS):
        base = base[: base.rfind(".")]
    return safe_stem(base, "audio") + ".mp3"


def check_upload(filename: Optional[str], content_type: Optional[str], head: bytes) -> None:
    """Explicit type checks: extension, declared type and an ISO BMFF 'ftyp' box at offset 0."""
    if not filename or not filename.lower().endswith(UPLOAD_EXTS):
        raise ConvertError("not_mp4")
    if (content_type or "").split(";")[0].strip().lower() not in UPLOAD_TYPES:
        raise ConvertError("not_mp4")
    if len(head) < 12 or head[4:8] != b"ftyp":
        raise ConvertError("not_mp4")
    box = int.from_bytes(head[0:4], "big")
    if not 12 <= box <= 4096 or not all(0x20 <= b < 0x7F for b in head[8:12]):
        raise ConvertError("not_mp4")


def probe_argv(tools: Tools, path: Path, forced: bool = True) -> list:
    opts = _INPUT_OPTS if forced else ["-protocol_whitelist", "file", "-f", "mp3"]
    return [*tools.ffprobe, "-hide_banner", "-loglevel", "error", "-max_alloc", MAX_ALLOC, *opts,
            "-show_entries", "format=format_name,duration,start_time:stream=codec_type,codec_name,channels,duration,start_time",
            "-of", "json", "-i", f"file:{path}"]


def convert_argv(tools: Tools, src: Path, dst: Path, limits: Limits, channels: int) -> list:
    argv = [*tools.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
            "-max_alloc", MAX_ALLOC, *_INPUT_OPTS, "-i", f"file:{src}",
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact",
            "-af", "aresample=async=1:first_pts=0", "-c:a", "libmp3lame", "-b:a", BITRATE]
    if channels > 2:
        argv += ["-ac", "2"]
    return argv + ["-t", str(limits.max_duration), "-fs", str(output_bound(limits)),
                   "-id3v2_version", "0", "-write_id3v1", "0", "-f", "mp3", "-n", f"file:{dst}"]


def output_bound(limits: Limits) -> int:
    """MP3 size cap passed to FFmpeg, with one second of bitrate headroom."""
    return (limits.max_duration + 1) * BYTES_PER_SECOND


def conversion_reservation(length: int, limits: Limits) -> int:
    # The multipart spool and our input file overlap while the upload is copied.
    return 2 * length + output_bound(limits) + 64 * 1024


def _probe(tools: Tools, path: Path, limits: Limits, cancel, forced=True) -> dict:
    result = run_bounded(probe_argv(tools, path, forced), cwd=str(path.parent), env=child_env(path.parent),
                         timeout=limits.probe_timeout, memory_bytes=limits.memory_bytes, max_processes=1,
                         cancel=cancel)
    if result.stopped:
        raise ConvertError("cancelled" if result.stopped == "cancelled" else "unreadable")
    if result.returncode != 0:
        raise ConvertError("unreadable")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise ConvertError("unreadable") from None
    return data if isinstance(data, dict) else {}


def _seconds(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number > 0 else 0.0  # rejects NaN


def _timestamp(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def convert(tools: Tools, src: Path, limits: Limits, cancel: threading.Event) -> tuple[Path, float]:
    """Convert src (in its own job folder) to an MP3 next to it. Returns (path, seconds)."""
    info = _probe(tools, src, limits, cancel)
    fmt = info.get("format") or {}
    if "mp4" not in str(fmt.get("format_name", "")).split(","):
        raise ConvertError("not_mp4")
    audio = [s for s in info.get("streams") or [] if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if not audio:
        raise ConvertError("no_audio")
    duration = _seconds(audio[0].get("duration"))
    # FFmpeg rebases timestamps to the file's start. Preserve a selected
    # track's later start as silence, without padding a whole-file offset.
    leading = max(0.0, _timestamp(audio[0].get("start_time")) - _timestamp(fmt.get("start_time")))
    seconds = duration + leading if duration > 0 else _seconds(fmt.get("duration"))
    if seconds <= 0:
        raise ConvertError("unreadable")
    if seconds > limits.max_duration:
        raise ConvertError("too_long")
    channels = int(audio[0].get("channels") or 2)

    part = src.with_name("output.part.mp3")
    final = src.with_name("output.mp3")
    result = run_bounded(convert_argv(tools, src, part, limits, channels), cwd=str(src.parent),
                         env=child_env(src.parent), timeout=limits.timeout,
                         memory_bytes=limits.memory_bytes, max_processes=1, cancel=cancel)
    try:
        if result.stopped:
            raise ConvertError("timeout" if result.stopped == "timeout" else "cancelled")
        if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            raise ConvertError("failed")
        # Verify what was actually written before publishing it.
        out = _probe(tools, part, limits, cancel, forced=False)
        streams = out.get("streams") or []
        got = _seconds((out.get("format") or {}).get("duration"))
        if [s.get("codec_name") for s in streams] != ["mp3"] or abs(got - seconds) > max(1.0, seconds * 0.02):
            raise ConvertError("failed")
        os.replace(part, final)
    finally:
        part.unlink(missing_ok=True)
    return final, got


@dataclass
class Item:
    id: str
    dir: Path
    path: Path
    name: str
    kind: str  # "mp3" or "zip"
    size: int
    seconds: float = 0.0
    created: float = 0.0


def unique_names(names: list) -> list:
    """Case-insensitively unique ZIP entry names: a.mp3, a (2).mp3, ..."""
    seen, out = set(), []
    for name in names:
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        candidate, n = name, 2
        while candidate.lower() in seen:
            candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
            n += 1
        seen.add(candidate.lower())
        out.append(candidate)
    return out


class Store:
    """Owns the runtime folder: one random subfolder per upload or ZIP."""

    def __init__(self, root: Path, limits: Limits):
        self.root, self.limits = Path(root), limits
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._items: dict[str, Item] = {}
        self._reserved = 0
        self._pins: dict[str, int] = {}
        self._removed: set[str] = set()
        self._deleting: set[str] = set()
        self._zip_slot = threading.BoundedSemaphore(1)

    def new_dir(self) -> Path:
        path = self.root / secrets.token_hex(16)
        path.mkdir(mode=0o700)
        return path

    def reserve(self, nbytes: int) -> bool:
        with self._lock:
            used = sum(i.size for i in self._items.values()) + self._reserved
            if used + nbytes > self.limits.max_disk_bytes or len(self._items) >= self.limits.max_items:
                return False
            self._reserved += nbytes
            return True

    def release(self, nbytes: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - nbytes)

    def add(self, item: Item, cancel: Callable[[], bool] = lambda: False) -> None:
        item.created = time.monotonic()
        with self._lock:
            if cancel():
                raise ConvertError("cancelled")
            if len(self._items) >= self.limits.max_items or item.id in self._items:
                raise ConvertError("busy")
            self._items[item.id] = item

    def get(self, item_id: str) -> Optional[Item]:
        with self._lock:
            return self._items.get(item_id) if item_id not in self._removed else None

    def pin(self, ids: list, kind: str | None = None) -> list[Item]:
        with self._lock:
            items = [self._items.get(i) if i not in self._removed else None for i in ids]
            if not items or any(i is None or (kind is not None and i.kind != kind) for i in items):
                raise ConvertError("not_found")
            for item in items:
                self._pins[item.id] = self._pins.get(item.id, 0) + 1
            return items

    def unpin(self, items: list[Item]) -> None:
        with self._lock:
            for item in items:
                count = self._pins.get(item.id, 0) - 1
                if count <= 0:
                    self._pins.pop(item.id, None)
                else:
                    self._pins[item.id] = count
        self._remove_pending()

    def _remove_pending(self) -> None:
        with self._lock:
            ready = [self._items[i] for i in self._removed if not self._pins.get(i) and i not in self._deleting]
            self._deleting.update(item.id for item in ready)
        for item in ready:
            removed = False
            try:
                shutil.rmtree(item.dir)
                removed = True
            except FileNotFoundError:
                removed = True
            except OSError:
                pass
            with self._lock:
                self._deleting.discard(item.id)
                if removed:
                    self._items.pop(item.id, None)
                    self._removed.discard(item.id)

    def drop(self, items: list) -> None:
        with self._lock:
            for item in items:
                if item.id in self._items:
                    self._removed.add(item.id)
        self._remove_pending()

    def clear(self) -> None:
        with self._lock:
            items = list(self._items.values())
        self.drop(items)

    def sweep(self) -> None:
        now = time.monotonic()
        with self._lock:
            old = [i for i in self._items.values() if now - i.created >= self.limits.ttl]
        self.drop(old)

    def build_zip(self, ids: list, cancel: Callable[[], bool] = lambda: False) -> Item:
        if not self._zip_slot.acquire(blocking=False):
            raise ConvertError("busy")
        items, job_dir, reserved = [], None, 0
        try:
            # The page only uses the newest ZIP. Retire older ones first so repeated
            # "Download all" clicks do not stack full-size copies in the disk budget.
            # A ZIP still being downloaded is pinned and is removed when its reader closes.
            with self._lock:
                old_zips = [i for i in self._items.values() if i.kind == "zip" and i.id not in self._removed]
            self.drop(old_zips)
            items = self.pin(ids, "mp3")
            names = unique_names([i.name for i in items])
            total = sum(i.path.stat().st_size for i in items)
            total += 4096 + sum(256 + 2 * len(name.encode("utf-8")) for name in names)
            if not self.reserve(total):
                raise ConvertError("disk_full")
            reserved = total
            job_dir = self.new_dir()
            part = job_dir / "files.part.zip"
            with zipfile.ZipFile(part, "x", compression=zipfile.ZIP_STORED) as zf:
                for item, arcname in zip(items, names):
                    if cancel():
                        raise ConvertError("cancelled")
                    info = zipfile.ZipInfo.from_file(item.path, arcname)
                    with open(item.path, "rb") as source, zf.open(info, "w", force_zip64=True) as target:
                        while chunk := source.read(1024 * 1024):
                            if cancel():
                                raise ConvertError("cancelled")
                            target.write(chunk)
            if cancel():
                raise ConvertError("cancelled")
            final = job_dir / "files.zip"
            os.replace(part, final)
            item = Item(secrets.token_hex(16), job_dir, final, "mp3-files.zip", "zip", final.stat().st_size)
            self.add(item, cancel)
            return item
        except BaseException:
            if job_dir is not None:
                shutil.rmtree(job_dir, ignore_errors=True)
            raise
        finally:
            self.release(reserved)
            self.unpin(items)
            self._zip_slot.release()

    def shutdown(self) -> None:
        self.clear()
        shutil.rmtree(self.root, ignore_errors=True)
