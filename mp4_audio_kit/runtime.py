"""Private runtime folder in the system temp dir, with cleanup of folders left by a killed app."""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

MARKER = ".runtime-marker"
STALE_SECONDS = 3600  # a running app refreshes its marker every minute


def touch(root: Path) -> None:
    (root / MARKER).write_bytes(b"")


def remove_stale(prefix: str, base: Optional[Path] = None, now: Optional[float] = None) -> int:
    """Delete our own abandoned runtime folders. Only folders with our prefix and marker qualify."""
    base = Path(base or tempfile.gettempdir())
    now = time.time() if now is None else now
    removed = 0
    try:
        candidates = [p for p in base.iterdir() if p.name.startswith(prefix)]
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            age = now - (path / MARKER).stat().st_mtime
        except OSError:
            continue  # no marker: not ours, leave it alone
        if age > STALE_SECONDS:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def create(prefix: str) -> Path:
    remove_stale(prefix)
    root = Path(tempfile.mkdtemp(prefix=prefix))
    touch(root)
    return root
