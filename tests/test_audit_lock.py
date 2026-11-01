"""A successful advisory command must not hide omitted or skipped lock entries."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def audit(tmp_path, monkeypatch):
    source = Path(__file__).with_name("audit_lock.py")
    spec = importlib.util.spec_from_file_location("owned_lock_audit", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    pin = " --hash=sha256:" + "a" * 64 + "\n"
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\n", "utf-8")
    (tmp_path / "requirements.txt").write_text("fixture-dep==1.0" + pin, "utf-8")
    (tmp_path / "requirements-tools.txt").write_text("-r requirements-bootstrap.txt\nsetuptools==84.2.1" + pin, "utf-8")
    (tmp_path / "requirements-bootstrap.txt").write_text("pip==26.2.2" + pin, "utf-8")
    return module


@pytest.mark.parametrize("change", ["complete", "omitted", "version", "skipped", "vulnerable"])
def test_zero_exit_still_requires_the_entire_safe_closure(audit, monkeypatch, change):
    dependencies = [{"name": name, "version": version, "vulns": []}
                    for name, version in (("fixture-dep", "1.0"), ("setuptools", "84.2.1"), ("pip", "26.2.2"))]
    if change == "omitted":
        dependencies.pop(0)
    elif change == "version":
        dependencies[0]["version"] = "0.9"
    elif change == "skipped":
        dependencies[0]["skip_reason"] = "could not audit"
    elif change == "vulnerable":
        dependencies[0]["vulns"] = [{"id": "SYNTHETIC-DO-NOT-IGNORE"}]

    def fake_command(command, **kwargs):
        included = {Path(command[index + 1]).name for index, value in enumerate(command[:-1]) if value == "-r"}
        assert included == {"requirements-dev.txt", "requirements.txt", "requirements-tools.txt", "requirements-bootstrap.txt"}
        Path(command[command.index("--output") + 1]).write_text(json.dumps({"dependencies": dependencies}), "utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(audit.subprocess, "run", fake_command)
    if change == "complete":
        audit.main()
    else:
        with pytest.raises(AssertionError):
            audit.main()


def test_unhashed_requirement_refuses_before_any_audit(audit):
    (audit.ROOT / "requirements.txt").write_text("fixture-dep==1.0\n", "utf-8")
    with pytest.raises(ValueError, match="exact, hashed"):
        audit.main()


def test_include_cannot_leave_repository(audit):
    (audit.ROOT / "requirements.txt").write_text("-r ../outside.txt\n", "utf-8")
    with pytest.raises(ValueError, match="inside this repository"):
        audit.main()
