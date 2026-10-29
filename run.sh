#!/bin/sh
# Creates .venv on first run (exact pinned, hash-checked packages), then starts the app.
set -eu
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -I -m venv .venv
fingerprint=$(.venv/bin/python -I -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path("requirements.txt").read_bytes()).hexdigest())')
installed=$(cat .venv/.installed 2>/dev/null || true)
if [ "$installed" != "$fingerprint" ]; then
    .venv/bin/python -I -m pip --isolated --disable-pip-version-check --require-virtualenv install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r requirements.txt
    printf '%s\n' "$fingerprint" > .venv/.installed
fi
exec .venv/bin/python -m mp4_audio_kit "$@"
