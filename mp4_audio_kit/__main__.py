"""Start the local app: python -m mp4_audio_kit"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

import uvicorn

from .app import create_app
from .converter import Limits, find_tools, tools_ready


def main() -> None:
    parser = argparse.ArgumentParser(prog="mp4_audio_kit", description="Local MP4 to MP3 converter.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument("--max-mb", type=int, default=2048, help="largest upload allowed per file")
    parser.add_argument("--max-minutes", type=int, default=360, help="longest video allowed")
    args = parser.parse_args()
    if not (1024 <= args.port <= 65535 and 1 <= args.max_mb <= 16384 and 1 <= args.max_minutes <= 1440):
        parser.error("value out of range")

    tools = find_tools()
    if tools is None:
        sys.exit("ffmpeg and ffprobe were not found on PATH. Install FFmpeg first (see README).")
    if not tools_ready(tools):
        sys.exit("FFmpeg startup check failed. Check your installation and put the actual FFmpeg bin folder "
                 "on PATH, ahead of any launcher shims, and try again (see README).")
    limits = Limits(max_file_bytes=args.max_mb * 1024 * 1024, max_duration=args.max_minutes * 60,
                    max_disk_bytes=max(Limits().max_disk_bytes, 4 * args.max_mb * 1024 * 1024))
    app = create_app(port=args.port, tools=tools, limits=limits)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"MP4 Audio Kit running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    uvicorn.run(
        app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False,
        server_header=False, proxy_headers=False, ws="none", http="h11", lifespan="on",
        limit_concurrency=64, timeout_keep_alive=5, timeout_graceful_shutdown=5,
        h11_max_incomplete_event_size=16384,
    )


if __name__ == "__main__":
    main()
