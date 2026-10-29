# MP4 Audio Kit

A small local web app that turns MP4 videos into MP3 audio. Drag one or more MP4 files onto the page (or press **Choose files**), then download each MP3, or all of them at once as a ZIP. It runs only on your computer at `http://127.0.0.1:8766`. There is no account, no API key, no telemetry and no hosted version.

Conversion is done by [FFmpeg](https://ffmpeg.org/) on your machine.

## Requirements

- Python 3.11 or newer.
- FFmpeg and ffprobe on `PATH`, built with `libmp3lame` (most builds include it). Use a current, patched build from a distributor linked on the [official download page](https://ffmpeg.org/download.html).
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Debian or Ubuntu: `sudo apt install ffmpeg`

## Start

Windows (PowerShell):

```powershell
.\run.ps1
```

macOS or Linux:

```sh
sh run.sh
```

The first run creates a `.venv` folder and installs the exact pinned packages from `requirements.txt` with hash checking from the official PyPI index. A changed requirements file triggers another checked install on the next launch. The app then opens in your browser. Press Ctrl+C in the terminal to stop it.

Manual equivalent:

```sh
python -m venv .venv
.venv/bin/python -I -m pip --isolated install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r requirements.txt   # Windows: .venv\Scripts\python.exe
.venv/bin/python -m mp4_audio_kit
```

Options (all optional):

| Flag | Default | Meaning |
| --- | --- | --- |
| `--port N` | 8766 | Local port |
| `--no-browser` | off | Do not open a browser tab |
| `--max-mb N` | 2048 | Largest MP4 allowed per file |
| `--max-minutes N` | 360 | Longest video allowed |

## What you get

- One MP3 per video: the first audio track, MP3 (LAME) at 192 kbit/s constant bitrate. More than two channels are mixed down to stereo.
- Gaps in the audio timestamps become silence, so recordings keep their timing. An MP3 can be larger than its input; `--max-mb` limits uploads, while output space is reserved from the duration limit and fixed bitrate.
- No tags: titles, artist, chapters and other metadata are not copied, and no ID3 tag is written.
- Download names come from the original file name, made safe. In a ZIP, repeated names become `name (2).mp3`, `name (3).mp3` and so on.
- Files that fail (not an MP4, damaged, no audio, too large, too long) show a short reason. Other files in the same batch carry on.
- Up to 50 files per browser batch, 200 retained results per app session, and one ZIP build at a time. Clear the list before starting another batch.
- Files added while **Clear list** finishes stay queued until the server confirms the clear. If it fails, press **Clear list** to retry.

## How it works

1. The browser uploads each file separately (two at a time) to the local app. Only file contents are sent. The app never accepts paths or URLs.
2. Each upload must be `.mp4` or `.m4v`, declare a video or generic binary type, and start with an MP4 `ftyp` box. It is stored under a fixed name in a new random folder.
3. ffprobe and ffmpeg run with a fixed command line, no shell and no stdin. The input is read with the MOV/MP4 demuxer forced, external track references off (`-enable_drefs 0 -use_absolute_path 0`) and only the `file` protocol allowed. There are time, size, memory and allocation limits.
4. The MP3 is written to a temporary name, checked with ffprobe (codec and duration), then renamed into place. The uploaded MP4 is deleted right after conversion. Finished MP3s expire after 30 minutes, when you press **Clear list**, or when the app stops. Clear also cancels in-flight work. A file already being downloaded stays until that reader closes; its size still counts against the space budget.

See [SECURITY.md](SECURITY.md) for the full list of controls.

## Tests

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
python -m pytest -q
```

The tests make their own synthetic MP4 files with FFmpeg (sine tones over solid colour video) and check the real MP3 output: codec, duration, bitrate-based size, absence of tags, and the actual tone frequency after decoding. They also cover forged and damaged uploads, multipart abuse, path and option-like file names, Host, Origin and CSRF checks, client disconnects, timeouts, mixed-result batches and repeated names.

Browser check (needs Node.js, Playwright and Chrome; all traffic outside the app is blocked):

```sh
PYTHON=.venv/bin/python PLAYWRIGHT_MODULE=/path/to/node_modules/playwright CHROME_PATH=/path/to/chrome node tests/browser/ui_check.cjs
```

## Third-party software

Installed by pip, not copied into this repository: Starlette (BSD-3-Clause), Uvicorn (BSD-3-Clause), python-multipart (Apache-2.0), AnyIO (MIT), h11 (MIT), Click (BSD-3-Clause), idna (BSD-3-Clause), typing_extensions (PSF-2.0). FFmpeg is a separate program you install yourself, under its own license (LGPL or GPL depending on the build).

## Feedback

[Send feedback](https://github.com/Jgolding1998/Jgolding1998/issues/new?template=feedback.yml)

## License

MIT, see [LICENSE](LICENSE).
