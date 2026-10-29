"""Loopback web app: MP4 uploads in, MP3 (or a ZIP of MP3s) out."""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import shutil
import threading
from importlib import resources
from pathlib import Path

import anyio
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .converter import (MESSAGES, ConvertError, Item, Limits, Store, Tools, check_upload, convert,
                        conversion_reservation, display_name)
from . import runtime
from .guard import BodyTooLarge, LocalGuard

_ID = re.compile(r"[0-9a-f]{32}")
_STATIC = {"/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
_STATUS = {"not_mp4": 415, "too_large": 413, "busy": 503, "disk_full": 507, "not_found": 404,
           "timeout": 422, "too_long": 422, "no_audio": 422, "unreadable": 422, "failed": 422,
           "bad_upload": 400, "bad_request": 400, "cancelled": 499}
MULTIPART_OVERHEAD = 64 * 1024


def _size_text(nbytes: int) -> str:
    gib = nbytes / 1024**3
    return f"{gib:g} GB" if gib >= 1 else f"{nbytes // (1024 * 1024)} MB"


def _error(code: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": MESSAGES.get(code, MESSAGES["failed"])},
                        status_code=_STATUS.get(code, 400))


def _copy_upload(upload: UploadFile, dst: Path, limit: int, cancel=None) -> bytes:
    """Copy the spooled upload to our own fixed filename. Returns the first bytes."""
    src = upload.file
    src.seek(0)
    head = src.read(64)
    written = len(head)
    if written > limit:
        raise ConvertError("too_large")
    with open(dst, "xb") as out:
        if cancel is not None and cancel.is_set():
            raise ConvertError("cancelled")
        out.write(head)
        while chunk := src.read(1024 * 1024):
            if cancel is not None and cancel.is_set():
                raise ConvertError("cancelled")
            written += len(chunk)
            if written > limit:
                raise ConvertError("too_large")
            out.write(chunk)
    return head


def create_app(*, port: int, tools: Tools, limits: Limits | None = None, root: Path | None = None):
    token = secrets.token_urlsafe(32)
    limits = limits or Limits()
    owned = root is None
    root = runtime.create("mp4-audio-kit-") if owned else Path(root)
    store = Store(root, limits)
    convert_slots = threading.BoundedSemaphore(limits.max_concurrent)
    uploads = {"active": 0}
    active_cancels = set()

    static_dir = resources.files(__package__) / "static"
    limits_line = (f"Up to {_size_text(limits.max_file_bytes)} and "
                   f"{limits.max_duration // 60} minutes per file. MP3 up to 192 kbps, metadata removed.")
    index_html = ((static_dir / "index.html").read_text("utf-8")
                  .replace("{{CSRF}}", token).replace("{{LIMITS}}", limits_line)
                  .replace("{{MAX_BYTES}}", str(limits.max_file_bytes)))
    static_files = {path: ((static_dir / name).read_bytes(), media) for path, (name, media) in _STATIC.items()}

    async def index(request: Request) -> Response:
        return Response(index_html, media_type="text/html; charset=utf-8")

    async def static(request: Request) -> Response:
        body, media = static_files[request.url.path]
        return Response(body, media_type=media)

    async def favicon(request: Request) -> Response:
        return Response(status_code=204)

    async def run_cancellable(request: Request, fn, cancel: threading.Event):
        """Run fn in a thread; a client disconnect sets cancel so the child process is stopped."""
        result = {}

        async def worker(scope):
            try:
                result["value"] = await anyio.to_thread.run_sync(fn)
            except BaseException as exc:  # re-raised below
                result["error"] = exc
            finally:
                scope.cancel()

        already_registered = cancel in active_cancels
        active_cancels.add(cancel)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(worker, tg.cancel_scope)
                try:
                    while True:
                        if await request.is_disconnected():
                            cancel.set()
                            break
                        await anyio.sleep(0.25)
                except BaseException:
                    if not result:
                        cancel.set()
                    raise
        finally:
            if not already_registered:
                active_cancels.discard(cancel)
        if "error" in result:
            raise result["error"]
        return result.get("value")

    async def convert_upload(request: Request) -> Response:
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return _error("not_mp4")
        length = int(request.headers["content-length"])
        if uploads["active"] >= limits.max_uploads:
            return _error("busy")
        reservation = conversion_reservation(length, limits)
        if not store.reserve(reservation):
            return _error("disk_full")
        uploads["active"] += 1
        job_dir = None
        keep = False
        cancel = threading.Event()
        active_cancels.add(cancel)
        try:
            job_dir = store.new_dir()
            try:
                form = await request.form(max_files=1, max_fields=0, max_part_size=1024)
            except (MultiPartException, HTTPException, KeyError, ValueError):
                return _error("bad_upload")
            except BodyTooLarge:
                return _error("too_large")
            except ClientDisconnect:
                return Response(status_code=499)
            try:
                keys = list(form.keys())
                upload = form.get("file")
                if keys != ["file"] or not isinstance(upload, UploadFile):
                    return _error("bad_upload")
                name = display_name(upload.filename)
                src = job_dir / "input.mp4"
                head = await run_cancellable(request, lambda: _copy_upload(upload, src, limits.max_file_bytes, cancel), cancel)
                check_upload(upload.filename, upload.content_type, head)
            finally:
                with anyio.CancelScope(shield=True):
                    await form.close()

            def work():
                with contextlib.ExitStack() as stack:
                    while not convert_slots.acquire(timeout=0.25):
                        if cancel.is_set():
                            raise ConvertError("cancelled")
                    stack.callback(convert_slots.release)
                    try:
                        return convert(tools, src, limits, cancel)
                    finally:
                        src.unlink(missing_ok=True)

            path, seconds = await run_cancellable(request, work, cancel)
            if cancel.is_set():
                return Response(status_code=499)
            item = Item(job_dir.name, job_dir, path, name, "mp3", path.stat().st_size, seconds)
            store.add(item, cancel.is_set)
            keep = True
            return JSONResponse({"id": item.id, "name": item.name, "bytes": item.size,
                                 "seconds": round(seconds, 2)})
        except ConvertError as exc:
            return _error(exc.code)
        except Exception:
            return _error("failed")
        finally:
            uploads["active"] -= 1
            active_cancels.discard(cancel)
            store.release(reservation)
            if job_dir is not None and not keep:
                shutil.rmtree(job_dir, ignore_errors=True)

    async def read_ids(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return None
        try:
            data = json.loads(await request.body())
        except (BodyTooLarge, ValueError, RecursionError):
            return None
        ids = data.get("ids") if isinstance(data, dict) and set(data) == {"ids"} else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= limits.max_zip_items
                or not all(isinstance(i, str) and _ID.fullmatch(i) for i in ids) or len(set(ids)) != len(ids)):
            return None
        return ids

    async def make_zip(request: Request) -> Response:
        ids = await read_ids(request)
        if ids is None:
            return _error("bad_request")
        cancel = threading.Event()
        try:
            item = await run_cancellable(request, lambda: store.build_zip(ids, cancel.is_set), cancel)
        except ConvertError as exc:
            return _error(exc.code)
        except Exception:
            return _error("failed")
        if cancel.is_set():
            store.drop([item])
            return Response(status_code=499)
        return JSONResponse({"id": item.id, "name": item.name, "bytes": item.size})

    async def clear(request: Request) -> Response:
        for cancel in tuple(active_cancels):
            cancel.set()
        await anyio.to_thread.run_sync(store.clear)
        return JSONResponse({"ok": True})

    async def download(request: Request) -> Response:
        item_id = request.path_params["item_id"]
        try:
            items = store.pin([item_id]) if _ID.fullmatch(item_id) else []
        except ConvertError:
            items = []
        if not items:
            return _error("not_found")
        item = items[0]
        if not item.path.is_file():
            store.unpin(items)
            return _error("not_found")
        media = "audio/mpeg" if item.kind == "mp3" else "application/zip"

        class DownloadResponse(FileResponse):
            async def __call__(self, scope, receive, send):
                try:
                    return await super().__call__(scope, receive, send)
                finally:
                    with anyio.CancelScope(shield=True):
                        await anyio.to_thread.run_sync(store.unpin, items)

        return DownloadResponse(item.path, filename=item.name, media_type=media)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async def sweeper():
            while True:
                await anyio.sleep(60)
                await anyio.to_thread.run_sync(store.sweep)
                if owned:
                    runtime.touch(root)

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(sweeper)
                try:
                    yield
                finally:
                    tg.cancel_scope.cancel()
        finally:
            for cancel in tuple(active_cancels):
                cancel.set()
            await anyio.to_thread.run_sync(store.shutdown)

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/app.js", static, methods=["GET"]),
        Route("/style.css", static, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/api/convert", convert_upload, methods=["POST"]),
        Route("/api/zip", make_zip, methods=["POST"]),
        Route("/api/clear", clear, methods=["POST"]),
        Route("/api/download/{item_id}", download, methods=["GET"]),
    ]
    async def unexpected(request, exception):
        return JSONResponse({"error": "failed", "message": "The request failed."}, status_code=500)

    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={Exception: unexpected})
    app.state.store = store
    app.state.token = token

    def body_limit(path: str) -> int:
        return limits.max_file_bytes + MULTIPART_OVERHEAD if path == "/api/convert" else 16 * 1024

    guarded = LocalGuard(app, port=port, token=token, body_limit=body_limit)
    guarded.state = app.state
    return guarded
