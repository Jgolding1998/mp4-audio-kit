import json
import os
import subprocess
import sys
import threading
import time

import pytest

from mp4_audio_kit import procguard
from test_procguard import alive, env

BASE = getattr(sys, "_base_executable", None) or sys.executable
WINDOWS = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object startup")


def target(marker):
    return [BASE, "-I", "-c", f"open({str(marker)!r},'w').write('started')"]


@WINDOWS
def test_target_waits_for_job_assignment_with_one_tool_process(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    assign = procguard._WindowsJob.assign

    def delayed(self, pid):
        time.sleep(0.3)
        assert not marker.exists()
        assign(self, pid)

    monkeypatch.setattr(procguard._WindowsJob, "assign", delayed)
    result = procguard.run_bounded(target(marker), cwd=str(tmp_path), env=env(), timeout=10, max_processes=1)
    assert result.returncode == 0 and result.stopped is None and marker.read_text() == "started"


@WINDOWS
def test_one_tool_process_limit_still_blocks_its_child(tmp_path):
    marker = tmp_path / "unexpected-child"
    code = ("import subprocess,sys\ntry:\n"
            f" subprocess.check_call({target(marker)!r})\n"
            "except OSError:\n print('blocked')\nelse:\n raise SystemExit('Child escaped')\n")
    result = procguard.run_bounded([BASE, "-I", "-c", code], cwd=str(tmp_path), env=env(),
                                   timeout=10, max_processes=1)
    assert result.returncode == 0 and result.stdout.strip() == b"blocked" and not marker.exists()


@WINDOWS
@pytest.mark.parametrize("stop", ["denied", "cancelled", "timeout"])
def test_stopped_assignment_never_starts_the_tool_and_closes_resources(tmp_path, monkeypatch, stop):
    marker = tmp_path / "started"
    cancel = threading.Event()
    processes, jobs = [], []
    popen, assign = procguard.subprocess.Popen, procguard._WindowsJob.assign

    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    def controlled(self, pid):
        jobs.append(self)
        if stop == "denied":
            raise OSError("Synthetic assignment failure")
        if stop == "timeout":
            time.sleep(0.2)
        assign(self, pid)
        if stop == "cancelled":
            cancel.set()

    monkeypatch.setattr(procguard.subprocess, "Popen", record)
    monkeypatch.setattr(procguard._WindowsJob, "assign", controlled)
    if stop == "denied":
        with pytest.raises(OSError, match="Synthetic assignment failure"):
            procguard.run_bounded(target(marker), cwd=str(tmp_path), env=env(), timeout=10,
                                  cancel=cancel, max_processes=1)
    else:
        result = procguard.run_bounded(target(marker), cwd=str(tmp_path), env=env(),
                                       timeout=0.05 if stop == "timeout" else 10,
                                       cancel=cancel, max_processes=1)
        assert result.stopped == stop
    assert not marker.exists() and all(job.handle is None for job in jobs)
    assert all(process.poll() is not None and process.stdin.closed and process.stdout.closed
               and process.stderr.closed for process in processes)


@pytest.mark.parametrize("stop", ["cancelled", "timeout"])
def test_preexisting_stop_does_not_launch(tmp_path, monkeypatch, stop):
    cancel = threading.Event()
    if stop == "cancelled":
        cancel.set()

    def forbidden(*args, **kwargs):
        raise AssertionError("Stopped work must not launch")

    monkeypatch.setattr(procguard.subprocess, "Popen", forbidden)
    result = procguard.run_bounded(target(tmp_path / "started"), cwd=str(tmp_path), env=env(),
                                   timeout=0 if stop == "timeout" else 10, cancel=cancel)
    assert result.stopped == stop and result.returncode is None


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), True, 10**400])
def test_nonfinite_timeout_cannot_create_unbounded_work(tmp_path, timeout):
    with pytest.raises(ValueError):
        procguard.run_bounded(target(tmp_path / "started"), cwd=str(tmp_path), env=env(), timeout=timeout)


@WINDOWS
def test_gate_ignores_import_shadowing_and_preserves_argv_stdin_and_exit(tmp_path):
    marker = tmp_path / "unexpected-import"
    (tmp_path / "subprocess.py").write_text(f"open({str(marker)!r},'w').write('bad')", encoding="utf-8")
    values = ["--flag", "spaces here", "$(no-shell)", ";"]
    child_env = {**env(), "PYTHONPATH": str(tmp_path), "PYTHONHOME": str(tmp_path), "OWNED_SENTINEL": "synthetic"}
    code = "import json,sys,os;print(json.dumps([sys.argv[1:],sys.stdin.read(),os.environ['OWNED_SENTINEL']]));sys.stderr.write('err');sys.exit(7)"
    result = procguard.run_bounded([BASE, "-I", "-c", code, *values], cwd=str(tmp_path), env=child_env,
                                   timeout=10, max_processes=1)
    assert result.returncode == 7 and result.stderr == b"err" and result.stopped is None
    assert json.loads(result.stdout) == [values, "", "synthetic"] and not marker.exists()


@WINDOWS
def test_gate_eof_never_releases_target(tmp_path):
    marker = tmp_path / "started"
    result = subprocess.run([BASE, "-I", "-S", "-c", procguard._WINDOWS_GATE, *target(marker)],
                            cwd=tmp_path, env=env(), input=b"", capture_output=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 125 and not marker.exists()


@WINDOWS
@pytest.mark.skipif(os.path.normcase(sys.executable) == os.path.normcase(BASE), reason="Needs a venv launcher")
def test_launcher_identity_mismatch_never_releases_target(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    monkeypatch.setattr(procguard.sys, "_base_executable", sys.executable)
    assign = procguard._WindowsJob.assign

    def delayed(self, pid):
        time.sleep(0.3)
        assign(self, pid)

    monkeypatch.setattr(procguard._WindowsJob, "assign", delayed)
    result = procguard.run_bounded(target(marker), cwd=str(tmp_path), env=env(), timeout=10, max_processes=1)
    assert result.returncode == 125 and not marker.exists()


@WINDOWS
@pytest.mark.skipif(os.path.normcase(sys.executable) == os.path.normcase(BASE), reason="Needs a venv launcher")
@pytest.mark.parametrize("stop", ["denied", "cancelled", "timeout"])
def test_launcher_failure_reaps_its_unreleased_gate(tmp_path, monkeypatch, stop):
    marker, gate_pid = tmp_path / "started", tmp_path / "owned-gate.pid"
    cancel = threading.Event()
    assign = procguard._WindowsJob.assign
    gate = f"import os\nopen({str(gate_pid)!r},'w').write(str(os.getpid()))\n" + procguard._WINDOWS_GATE
    monkeypatch.setattr(procguard.sys, "_base_executable", sys.executable)
    monkeypatch.setattr(procguard, "_WINDOWS_GATE", gate)

    def delayed(self, pid):
        time.sleep(0.3)
        if stop == "denied":
            raise OSError("Synthetic assignment failure")
        assign(self, pid)
        if stop == "cancelled":
            cancel.set()

    monkeypatch.setattr(procguard._WindowsJob, "assign", delayed)
    kid = None
    try:
        if stop == "denied":
            with pytest.raises(OSError, match="Synthetic assignment failure"):
                procguard.run_bounded(target(marker), cwd=str(tmp_path), env=env(), timeout=10, max_processes=1)
        else:
            result = procguard.run_bounded(target(marker), cwd=str(tmp_path), env=env(),
                                           timeout=0.05 if stop == "timeout" else 10,
                                           max_processes=1, cancel=cancel)
            assert result.stopped == stop
        kid = int(gate_pid.read_text())
        assert not marker.exists() and not alive(kid)
    finally:
        if kid is None and gate_pid.exists():
            kid = int(gate_pid.read_text())
        if kid is not None and alive(kid):
            import signal
            os.kill(kid, signal.SIGTERM)


@WINDOWS
def test_helper_exit_before_release_returns_its_status(tmp_path, monkeypatch):
    import signal

    marker = tmp_path / "started"
    assign = procguard._WindowsJob.assign

    def exited(self, pid):
        assign(self, pid)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)

    monkeypatch.setattr(procguard._WindowsJob, "assign", exited)
    result = procguard.run_bounded(target(marker), cwd=str(tmp_path), env=env(), timeout=10, max_processes=1)
    assert result.returncode != 0 and result.stopped is None and not marker.exists()


@WINDOWS
@pytest.mark.parametrize("code", [0xC0000005, 0xFFFFFFFF])
def test_gate_preserves_windows_exit_status_bits(tmp_path, code):
    command = ("import ctypes;kernel=ctypes.WinDLL('kernel32');"
               "kernel.ExitProcess.argtypes=[ctypes.c_uint];"
               f"kernel.ExitProcess({code})")
    result = procguard.run_bounded([BASE, "-I", "-c", command], cwd=str(tmp_path), env=env(),
                                   timeout=10, max_processes=1)
    assert result.returncode == code and result.stopped is None


@WINDOWS
def test_watcher_failure_reaps_descendants_and_closes_pipes(tmp_path, monkeypatch):
    marker = tmp_path / "owned-child.pid"
    processes = []
    popen = procguard.subprocess.Popen

    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    def watch():
        if marker.exists():
            raise RuntimeError("Synthetic watcher failure")

    code = ("import subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(30)'])\n"
            f"open({str(marker)!r},'w').write(str(child.pid))\ntime.sleep(30)\n")
    monkeypatch.setattr(procguard.subprocess, "Popen", record)
    kid = None
    try:
        with pytest.raises(RuntimeError, match="Synthetic watcher failure"):
            procguard.run_bounded([BASE, "-I", "-c", code], cwd=str(tmp_path), env=env(), timeout=10,
                                  watch=watch, max_processes=2)
        kid = int(marker.read_text())
        assert not alive(kid)
        assert all(process.poll() is not None and process.stdout.closed and process.stderr.closed for process in processes)
    finally:
        if kid is None and marker.exists():
            kid = int(marker.read_text())
        if kid is not None and alive(kid):
            import signal
            os.kill(kid, signal.SIGTERM)
