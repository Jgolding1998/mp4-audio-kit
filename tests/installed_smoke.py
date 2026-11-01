"""Run only from the isolated installed-wheel check, with fake processing."""
import hashlib
from importlib import import_module, resources
from importlib.metadata import distribution
import io
import json
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch
import zipfile

SOURCE = Path(sys.argv[1]).resolve()
CHECKS = []


def check(condition, name):
    if not condition:
        raise AssertionError(name)
    CHECKS.append(name)


def installed(package, assets):
    module = import_module(package)
    location = Path(module.__file__).resolve()
    check(location.is_relative_to(Path(sys.prefix).resolve()) and not location.is_relative_to(SOURCE),
          "imports from installed site-packages outside checkout")
    files = resources.files(package)
    metadata = distribution(package.replace("_", "-"))
    dependencies = {item.split("==", 1)[0] for item in metadata.requires or []}
    check(dependencies == {"starlette", "uvicorn", "python-multipart"}, "installed wheel declares every direct runtime dependency")
    hashes = {}
    for asset in assets:
        data = files.joinpath(asset).read_bytes()
        check(bool(data), "packaged resource " + asset)
        hashes[asset] = hashlib.sha256(data).hexdigest()
    return hashes


def headers(response):
    check(response.headers.get("cache-control") == "no-store", "responses are not cached")
    check("frame-ancestors 'none'" in response.headers.get("content-security-policy", ""),
          "frame protection retained")


from starlette.testclient import TestClient
from mp4_audio_kit import app as app_module
from mp4_audio_kit.converter import Limits, Tools

hashes = installed("mp4_audio_kit", ("static/index.html", "static/app.js", "static/style.css"))
payload = b"\0\0\0\x18ftypisom" + b"\0" * 64
output = b"OFFLINE-CONVERTER-FIXTURE"
calls = []


def fake_convert(tools, source, limits, cancel):
    check(source.read_bytes() == payload, "fixed owned input bytes reach fake converter")
    calls.append(source)
    target = source.with_name("output.mp3")
    target.write_bytes(output)
    return target, 1.0


app = app_module.create_app(port=18766, tools=Tools(("never-execute",), ("never-execute",)),
                           limits=Limits(max_duration=1), root=Path.cwd() / "synthetic data 空间")
with patch.object(app_module, "convert", fake_convert), TestClient(app, base_url="http://127.0.0.1:18766") as client:
    page = client.get("/")
    check(page.status_code == 200, "installed homepage works")
    headers(page)
    check(client.get("/app.js").status_code == client.get("/style.css").status_code == 200,
          "installed static routes work")
    check(client.get("/", headers={"Host": "foreign.invalid"}).status_code == 400, "foreign host refused")
    upload = {"file": ("../../CON.mp4", payload, "video/mp4")}
    check(client.post("/api/convert", files=upload).status_code == 403 and calls == [],
          "unauthorized upload refuses before converter")
    client.headers.update({"Origin": "http://127.0.0.1:18766", "X-CSRF-Token": app.state.token})
    response = client.post("/api/convert", files=upload)
    check(response.status_code == 200 and len(calls) == 1, "fake conversion completes without FFmpeg")
    item = response.json()
    check("/" not in item["name"] and "\\" not in item["name"], "download name cannot create a path")
    downloaded = client.get("/api/download/" + item["id"])
    check(downloaded.content == output, "installed download retains exact fake output")
    zipped = client.post("/api/zip", json={"ids": [item["id"]]})
    check(zipped.status_code == 200, "installed archive workflow completes")
    body = client.get("/api/download/" + zipped.json()["id"]).content
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        check(archive.namelist() == [item["name"]] and archive.read(item["name"]) == output,
              "archive uses safe names and exact output bytes")

print(json.dumps({"installed": True, "checks": CHECKS, "resources_sha256": hashes,
                  "native_tools_executed": False, "network_requests": False}))

