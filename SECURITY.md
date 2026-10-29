# Security

MP4 Audio Kit is a local, single-user app. It is meant to be safe to leave running while you browse other sites, and to run FFmpeg on untrusted MP4 files with as little reach as practical. It is not an operating-system sandbox.

## Reporting a vulnerability

Use [Security > Report a vulnerability](https://github.com/Jgolding1998/mp4-audio-kit/security/advisories/new) to send a private report. Do not submit vulnerability details through the public feedback form.

Include the affected version, platform, expected behavior and a synthetic reproduction. Leave out credentials, personal information and private files.

## Controls

Web server

- Listens on `127.0.0.1` only. Requests must carry `Host: 127.0.0.1:<port>` or `localhost:<port>`, which blocks DNS rebinding.
- Every non-GET request needs the page's own `Origin` and a random per-run `X-CSRF-Token`. Cross-site `Sec-Fetch-Site` values are refused.
- Request bodies need a declared length. Uploads are capped at the file limit plus 64 KB; other bodies at 16 KB. Bodies longer than declared are cut off.
- Strict Content-Security-Policy (`default-src 'none'`, self-hosted script and style only), `nosniff`, `no-referrer`, `DENY` framing, `no-store`. No inline script and no remote assets. Text is set with `textContent`.
- Error replies use fixed messages. FFmpeg output, paths and server details are never sent to the browser. Access logging is off.

Uploads

- Multipart parsing is done by Starlette with python-multipart: exactly one file field named `file`, no other fields, bounded part headers.
- Extension must be `.mp4` or `.m4v`; declared type must be `video/mp4`, `video/x-m4v` or `application/octet-stream`; the file must begin with an `ftyp` box. ffprobe must then report the MP4 family and at least one audio track.
- The upload is stored as `input.mp4` in a new random folder. The browser's file name is only used, after cleaning, as the download name. It never reaches a path or the FFmpeg command line.
- Limits: 2 GB per file, 6 hours of media, four uploads in flight, two conversions at once, 200 retained results and one ZIP build at a time. The default 8 GB file-content budget counts retained files and reservations for the multipart spool, the overlapping input copy, encoder output and ZIP overhead. Spool headroom is released after its file closes. Reservations can reject work before that limit is physically reached. Filesystem allocation and parser bookkeeping overhead are not part of this budget.
- Cleanup: the folder is removed on any error, refusal or client disconnect. Disconnects and server-task cancellation signal the conversion worker to stop. The upload is deleted after conversion; results expire after 30 minutes, on **Clear list**, or when the app stops. Clear cancels in-flight work and hides expired results immediately. Existing downloads keep their files pinned until the reader closes; pinned or failed-to-delete files remain charged to the space budget. A later sweep retries deletion failures. Folders left by a killed app are removed on the next start after an hour.

FFmpeg

- Fixed argument list, `shell=False`, `-nostdin`, minimal environment.
- Tool discovery uses absolute PATH directories, excluding the working directory and relative or empty entries. Windows tools must be actual `.exe` files. This does not establish that a configured tool or PATH directory is trustworthy.
- Input: `-protocol_whitelist file -f mov -enable_drefs 0 -use_absolute_path 0`, opened as `file:<fixed path>`. No network protocols, no other demuxers, no external track references.
- Output: first audio track only, `libmp3lame` at 192k, `-map_metadata -1 -map_chapters -1`, no ID3v1 or ID3v2, bit-exact flags, `-t` and `-fs` limits, `-n` (never overwrite), `-max_alloc` 512 MB.
- The MP3 is written to a temporary name, verified with ffprobe (codec `mp3`, duration within 1 second or 2% of the source, whichever is larger), then renamed atomically.
- Each encoder process has a 20-minute wall-clock timeout; each probe has 30 seconds. Waiting for a conversion slot can be cancelled. On Windows an isolated Python helper waits for Job Object assignment and verifies its process ID before starting FFmpeg or ffprobe. The helper shares the memory cap and occupies its own process slot; each tool still has a single-process limit and kill-on-close. On Linux an address-space limit is applied after start. On macOS only the time, size and allocation limits apply.

ZIP

- Only finished MP3s can be added. Entry names are the cleaned download names with no folders, made unique case-insensitively. Files are stored, not recompressed. The ZIP is built under a temporary name and renamed when complete. Sources stay pinned during the build; copying checks cancellation every 1 MB. Directory-creation and copy failures release the reservation and build slot. Starting a new ZIP retires older ZIPs from the disk budget; one that is still downloading is removed when that download ends.

Supply chain

- `requirements.txt` pins exact versions with sha256 hashes from PyPI. Install with `--require-hashes --only-binary=:all:`. CI uses actions pinned to commit SHAs, read-only permissions and no secrets.

## Known limits

- This is process containment, not a sandbox: FFmpeg runs as your user. Reading stream information can also parse video headers, even when the output is audio only. A bug in FFmpeg's demuxer or decoders could still be exploited by a crafted file. Use a maintained, patched build from a distributor linked on the [official download page](https://ffmpeg.org/download.html).
- Starlette buffers uploads larger than 1 MB in the system temp folder while the request is parsed, before they are copied into the app's own folder. That buffer is closed and deleted when the request ends.
- Process creation and Job Object assignment are operating-system calls that cannot be interrupted by the app's deadline. Cancellation and timeout are checked before releasing a Windows tool from its startup gate.
- Any local program running as your user can reach the loopback port. The CSRF token stops web pages, not local software.
