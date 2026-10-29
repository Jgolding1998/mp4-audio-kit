import os
import sys
import threading
import time

import pytest

from mp4_audio_kit.procguard import run_bounded

PY = [sys.executable, "-I", "-c"]


def env():
    keep = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR", "PATH")}
    return keep


def alive(pid):
    if os.name == "nt":
        import ctypes

        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_captures_output_and_exit(tmp_path):
    r = run_bounded(PY + ["import sys; print('hi'); sys.stderr.write('err'); sys.exit(3)"],
                    cwd=str(tmp_path), env=env(), timeout=20)
    assert r.returncode == 3 and r.stdout.strip() == b"hi" and r.stderr == b"err" and r.stopped is None


def test_running_child_uses_an_interruptible_completion_wait(tmp_path, monkeypatch):
    from mp4_audio_kit import procguard

    waits = []
    original = procguard.subprocess.Popen.wait

    def observed(proc, timeout=None):
        waits.append(timeout)
        return original(proc, timeout=timeout)

    monkeypatch.setattr(procguard.subprocess.Popen, "wait", observed)
    r = run_bounded(PY + ["import time; time.sleep(.15); print('complete')"],
                    cwd=str(tmp_path), env=env(), timeout=10)
    assert r.returncode == 0 and r.stdout.strip() == b"complete" and r.stopped is None
    assert any(wait is not None and 0 <= wait <= procguard.POLL_SECONDS for wait in waits)


def test_timeout_kills(tmp_path):
    start = time.monotonic()
    r = run_bounded(PY + ["import time; time.sleep(60)"], cwd=str(tmp_path), env=env(), timeout=1)
    assert r.stopped == "timeout" and time.monotonic() - start < 15


def test_output_is_capped(tmp_path):
    r = run_bounded(PY + ["import sys; sys.stdout.write('x' * 2_000_000)"],
                    cwd=str(tmp_path), env=env(), timeout=20, max_output=1000)
    assert len(r.stdout) == 1000


def test_no_stdin(tmp_path):
    r = run_bounded(PY + ["import sys; print(repr(sys.stdin.read()))"], cwd=str(tmp_path), env=env(), timeout=20)
    assert r.stdout.strip() == b"''"


def test_cancel_and_watch(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    r = run_bounded(PY + ["import time; time.sleep(60)"], cwd=str(tmp_path), env=env(), timeout=30, cancel=cancel)
    assert r.stopped == "cancelled"
    r = run_bounded(PY + ["import time; time.sleep(60)"], cwd=str(tmp_path), env=env(), timeout=30,
                    watch=lambda: "too_large")
    assert r.stopped == "too_large"


def test_lines_streamed(tmp_path):
    seen = []
    run_bounded(PY + ["print('a'); print('b')"], cwd=str(tmp_path), env=env(), timeout=20, on_stdout_line=seen.append)
    assert [s.strip() for s in seen] == [b"a", b"b"]


def test_grandchildren_are_killed(tmp_path):
    pid_file = tmp_path / "kid.pid"
    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(120)\n"
    )
    r = run_bounded(PY + [code], cwd=str(tmp_path), env=env(), timeout=3)
    assert r.stopped == "timeout"
    kid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while alive(kid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(kid)


def test_rejects_non_list_argv(tmp_path):
    with pytest.raises(ValueError):
        run_bounded("echo hi", cwd=str(tmp_path), env=env(), timeout=1)


def test_failing_line_callback_keeps_draining(tmp_path):
    def boom(line):
        raise RuntimeError("bad callback")

    r = run_bounded(PY + ["import sys\nfor i in range(20000): print('x' * 100)"], cwd=str(tmp_path), env=env(),
                    timeout=20, on_stdout_line=boom)
    assert r.stopped is None and r.returncode == 0
