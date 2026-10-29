"""Serve the real app with a chosen runtime folder, for browser tests only."""

import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp4_audio_kit.app import create_app  # noqa: E402
from mp4_audio_kit.converter import Limits, find_tools  # noqa: E402

port = int(sys.argv[1])
app = create_app(port=port, tools=find_tools(), limits=Limits(max_file_bytes=5 * 1024 * 1024),
                 root=Path(sys.argv[2]))
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="on",
            server_header=False, proxy_headers=False, ws="none", http="h11", limit_concurrency=64,
            timeout_keep_alive=5, timeout_graceful_shutdown=5, h11_max_incomplete_event_size=16384)
