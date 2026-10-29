import io
import json
import zipfile

import pytest

from conftest import ffprobe_json, needs_ffmpeg, runtime_dirs, upload

pytestmark = needs_ffmpeg


def test_index_has_token_and_headers(make_client):
    client = make_client()
    r = client.get("/")
    assert r.status_code == 200 and client.app_state.token in r.text and "{{" not in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert "server" not in r.headers
    for path in ("/app.js", "/style.css"):
        assert client.get(path).status_code == 200


def test_single_conversion_and_download(make_client, media, tmp_path):
    client = make_client()
    r = upload(client, media / "tone.mp4")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name"] == "tone.mp3" and abs(data["seconds"] - 3.0) < 0.15
    d = client.get(f"/api/download/{data['id']}")
    assert d.status_code == 200 and d.headers["content-type"] == "audio/mpeg"
    assert d.headers["content-disposition"] == 'attachment; filename="tone.mp3"'
    assert len(d.content) == data["bytes"] and b"SECRET" not in d.content
    saved = tmp_path / "check.mp3"
    saved.write_bytes(d.content)
    assert [s["codec_name"] for s in ffprobe_json(saved)["streams"]] == ["mp3"]
    # Only the published MP3 remains; the uploaded MP4 is gone.
    job = client.app_state.store.get(data["id"]).dir
    assert [p.name for p in job.iterdir()] == ["output.mp3"]


def test_batch_with_mixed_results_and_repeated_names(make_client, media):
    client = make_client()
    results = [
        upload(client, media / "tone.mp4", "tone.mp4"),
        upload(client, media / "notes.mp4", "notes.mp4"),
        upload(client, media / "tone.mp4", "tone.mp4"),
        upload(client, media / "silent.mp4", "silent.mp4"),
        upload(client, media / "surround.mp4", "TONE.mp4"),
        upload(client, media / "truncated.mp4", "broken.mp4"),
    ]
    codes = [r.status_code for r in results]
    assert codes == [200, 415, 200, 422, 200, 422]
    assert [results[i].json()["error"] for i in (1, 3, 5)] == ["not_mp4", "no_audio", "unreadable"]
    ok = [results[i].json() for i in (0, 2, 4)]
    assert len({o["id"] for o in ok}) == 3
    r = client.post("/api/zip", json={"ids": [o["id"] for o in ok]})
    assert r.status_code == 200
    z = client.get(f"/api/download/{r.json()['id']}")
    assert z.headers["content-type"] == "application/zip"
    assert z.headers["content-disposition"] == 'attachment; filename="mp3-files.zip"'
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert zf.namelist() == ["tone.mp3", "tone (2).mp3", "TONE (3).mp3"]
        assert all(zf.read(n)[:1] == b"\xff" for n in zf.namelist())
    # Failed uploads leave nothing behind: 3 MP3 folders + 1 ZIP folder.
    assert len(runtime_dirs(client)) == 4


def test_traversal_and_option_names_are_inert(make_client, media, tmp_path):
    client = make_client()
    for name, expected in (("../../../../evil.mp4", "evil.mp3"), ("-f lavfi -i x.mp4", "f lavfi -i x.mp3"),
                           ("..\\..\\evil2.mp4", "evil2.mp3"), ("CON.mp4", "_CON.mp3")):
        r = upload(client, media / "tone.mp4", name)
        assert r.status_code == 200 and r.json()["name"] == expected
    assert not list(tmp_path.rglob("evil*"))
    for d in runtime_dirs(client):
        assert len(d) == 32


@pytest.mark.parametrize("name, ctype", [
    ("tone.mov", "video/mp4"), ("tone.mp4", "text/plain"), ("tone.mp4", "video/webm"), ("tone", "video/mp4"),
])
def test_type_checks(make_client, media, name, ctype):
    client = make_client()
    r = upload(client, media / "tone.mp4", name, ctype)
    assert r.status_code == 415 and r.json()["error"] == "not_mp4"
    assert runtime_dirs(client) == []


@pytest.mark.parametrize("fixture", ["audio.webm", "audio.mp3", "notes.mp4"])
def test_forged_content(make_client, media, fixture):
    client = make_client()
    r = upload(client, media / fixture, "video.mp4")
    assert r.status_code == 415 and runtime_dirs(client) == []


def test_multipart_shape(make_client, media):
    client = make_client()
    data = (media / "tone.mp4").read_bytes()
    two = client.post("/api/convert", files=[("file", ("a.mp4", data, "video/mp4")), ("file", ("b.mp4", data, "video/mp4"))])
    extra = client.post("/api/convert", files={"file": ("a.mp4", data, "video/mp4")}, data={"path": "Z:/synthetic-fixtures/outside.mp4"})
    wrong = client.post("/api/convert", files={"upload": ("a.mp4", data, "video/mp4")})
    empty = client.post("/api/convert", content=b"", headers={"Content-Type": "multipart/form-data; boundary=x"})
    garbage = client.post("/api/convert", content=b"--x\r\nnonsense", headers={"Content-Type": "multipart/form-data; boundary=x"})
    noboundary = client.post("/api/convert", content=b"abc", headers={"Content-Type": "multipart/form-data"})
    for r in (two, extra, wrong, empty, garbage, noboundary):
        assert r.status_code == 400 and r.json()["error"] == "bad_upload", r.text
    raw = client.post("/api/convert", content=data, headers={"Content-Type": "video/mp4"})
    js = client.post("/api/convert", json={"path": "Z:/synthetic-fixtures/outside.mp4"})
    assert raw.status_code == 415 and js.status_code == 415
    assert runtime_dirs(client) == []


def test_oversized_upload(make_client, media):
    client = make_client(max_file_bytes=20_000)
    r = upload(client, media / "tone.mp4")
    assert r.status_code == 413 and runtime_dirs(client) == []


def test_disk_budget(make_client, media):
    client = make_client(max_disk_bytes=10_000)
    r = upload(client, media / "tone.mp4")
    assert r.status_code == 507 and r.json()["error"] == "disk_full"


def test_too_long_and_timeout(make_client, media):
    from conftest import slow_tools

    assert upload(make_client(max_duration=1), media / "tone.mp4").json()["error"] == "too_long"
    client = make_client(tools=slow_tools(), timeout=1)
    r = upload(client, media / "tone.mp4")
    assert r.status_code == 422 and r.json()["error"] == "timeout" and runtime_dirs(client) == []


@pytest.mark.parametrize("headers, status", [
    ({"Host": "evil.example"}, 400),
    ({"Host": "localhost:1"}, 400),
    ({"Origin": "http://evil.example"}, 403),
    ({"Origin": "http://127.0.0.1:8765"}, 403),
    ({"X-CSRF-Token": "nope"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
])
def test_csrf_origin_host(make_client, media, headers, status):
    client = make_client()
    with open(media / "tone.mp4", "rb") as f:
        r = client.post("/api/convert", files={"file": ("tone.mp4", f, "video/mp4")}, headers=headers)
    assert r.status_code == status and runtime_dirs(client) == []
    assert client.post("/api/clear", json={}, headers=headers).status_code == status


def test_localhost_origin_allowed(make_client, media):
    client = make_client()
    alt = "http://localhost:8766"
    with open(media / "tone.mp4", "rb") as f:
        r = client.post("/api/convert", files={"file": ("tone.mp4", f, "video/mp4")},
                        headers={"Host": "localhost:8766", "Origin": alt})
    assert r.status_code == 200


@pytest.mark.parametrize("body", [
    {"ids": []}, {"ids": "x"}, {"ids": ["../x"]}, {"ids": ["0" * 32, "0" * 32]}, {"ids": ["0" * 32], "x": 1}, [],
    {"ids": ["%032x" % i for i in range(51)]},
])
def test_zip_validation(make_client, body):
    client = make_client()
    r = client.post("/api/zip", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_zip_of_missing_items(make_client):
    client = make_client()
    r = client.post("/api/zip", json={"ids": ["0" * 32]})
    assert r.status_code == 404


def test_clear_and_unknown_download(make_client, media):
    client = make_client()
    item = upload(client, media / "tone.mp4").json()["id"]
    assert client.post("/api/clear", json={}).status_code == 200
    assert client.get(f"/api/download/{item}").status_code == 404
    assert runtime_dirs(client) == []
    for path in ("/api/download/../../etc", "/api/download/" + "z" * 32, "/static/../app.py"):
        assert client.get(path).status_code == 404


def test_shutdown_removes_runtime(make_client, media):
    client = make_client()
    upload(client, media / "tone.mp4")
    root = client.app_state.store.root
    client.__exit__(None, None, None)
    assert not root.exists()
    client.__enter__()
