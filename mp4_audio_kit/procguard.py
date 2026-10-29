"""Run one fixed argv in a bounded child process.

Bounds: wall-clock deadline, captured output size, optional watcher callback,
cancel event, and best-effort memory/process limits (Windows Job Object or
Linux prlimit). This is containment for runaway work, not an OS sandbox.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

POLL_SECONDS = 0.1
_WINDOWS_GATE = (
    "import os,subprocess,sys\n"
    "if sys.stdin.buffer.readline(32) != (str(os.getpid())+'\\n').encode('ascii'): raise SystemExit(125)\n"
    "code=subprocess.call(sys.argv[1:],stdin=subprocess.DEVNULL,"
    "shell=False,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)\n"
    "os._exit(code if code < 2**31 else code-2**32)\n"
)


@dataclass
class RunResult:
    returncode: Optional[int]
    stdout: bytes
    stderr: bytes
    stopped: Optional[str] = None  # "timeout", "cancelled" or a watcher reason


class _Capture(threading.Thread):
    """Drain a pipe into a capped buffer, optionally reporting complete lines."""

    def __init__(self, pipe, cap: int, on_line=None):
        super().__init__(daemon=True)
        self.pipe, self.cap, self.on_line = pipe, cap, on_line
        self.data = bytearray()
        self._partial = bytearray()

    def run(self) -> None:
        try:
            while True:
                chunk = self.pipe.read1(65536) if hasattr(self.pipe, "read1") else self.pipe.read(65536)
                if not chunk:
                    break
                room = self.cap - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
                if self.on_line is not None:
                    self._partial += chunk
                    while b"\n" in self._partial:
                        line, _, rest = self._partial.partition(b"\n")
                        self._partial = bytearray(rest)
                        try:
                            self.on_line(bytes(line[:65536]))
                        except Exception:
                            pass  # never stop draining, or the child would block
                    if len(self._partial) > 65536:
                        self._partial.clear()
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.pipe.close()
            except OSError:
                pass


class _WindowsJob:
    """Kill-on-close Job Object so a stop also ends grandchildren."""

    def __init__(self, memory_bytes: Optional[int], max_processes: int):
        import ctypes
        from ctypes import wintypes

        self._k32 = k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("CreateJobObject failed")
        info = Extended()
        flags = 0x2000 | 0x400 | 0x8  # KILL_ON_JOB_CLOSE | DIE_ON_UNHANDLED_EXCEPTION | ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = max_processes
        if memory_bytes:
            flags |= 0x200  # JOB_MEMORY
            info.JobMemoryLimit = memory_bytes
        info.BasicLimitInformation.LimitFlags = flags
        if not k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError("SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        proc = self._k32.OpenProcess(0x0001 | 0x0100, False, pid)  # TERMINATE | SET_QUOTA
        if not proc:
            raise OSError("OpenProcess failed")
        try:
            if not self._k32.AssignProcessToJobObject(self.handle, proc):
                raise OSError("AssignProcessToJobObject failed")
        finally:
            self._k32.CloseHandle(proc)

    def terminate(self) -> None:
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def _kill_tree(proc: subprocess.Popen, job: Optional[_WindowsJob]) -> None:
    if job is not None:
        job.terminate()
    elif os.name == "posix":
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_bounded(
    argv: list,
    *,
    cwd: str,
    env: dict,
    timeout: float,
    max_output: int = 256 * 1024,
    memory_bytes: Optional[int] = None,
    max_processes: int = 8,
    watch: Optional[Callable[[], Optional[str]]] = None,
    on_stdout_line: Optional[Callable[[bytes], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> RunResult:
    """Run argv (no shell, no stdin) and stop it on deadline, cancel or watcher reason."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    try:
        valid_timeout = isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and math.isfinite(timeout)
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise ValueError("timeout must be a finite number")
    deadline = time.monotonic() + timeout

    def stop_reason():
        if cancel is not None and cancel.is_set():
            return "cancelled"
        return "timeout" if time.monotonic() >= deadline else None

    stopped = stop_reason()
    if stopped:
        return RunResult(None, b"", b"", stopped)
    kwargs: dict = {}
    child_argv = argv
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        # A venv launcher can start a child before assignment, so the gate uses the base interpreter.
        base = getattr(sys, "_base_executable", None) or sys.executable
        child_argv = [base, "-I", "-S", "-c", _WINDOWS_GATE, *argv]
    else:
        kwargs["start_new_session"] = True

    job = None
    if os.name == "nt":
        job = _WindowsJob(memory_bytes, max_processes + 1)  # one dedicated gate slot
    try:
        proc = subprocess.Popen(
            child_argv, cwd=cwd, env=env, shell=False, close_fds=True,
            stdin=subprocess.PIPE if job is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
        )
    except BaseException:
        if job:
            job.close()
        raise
    out = err = None
    try:
        out = _Capture(proc.stdout, max_output, on_stdout_line)
        err = _Capture(proc.stderr, max_output)
        out.start()
        err.start()
        stopped = stop_reason()
        if job is not None and not stopped:
            job.assign(proc.pid)
            stopped = stop_reason()
            if not stopped:
                try:
                    proc.stdin.write((str(proc.pid) + "\n").encode("ascii"))
                    proc.stdin.close()
                except OSError:
                    pass  # a helper that already exited still has a return code to collect
        elif job is None and memory_bytes and sys.platform.startswith("linux"):
            import resource

            try:
                resource.prlimit(proc.pid, resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                resource.prlimit(proc.pid, resource.RLIMIT_CORE, (0, 0))
            except (OSError, ValueError):
                pass

        if stopped:
            _kill_tree(proc, job)
        while proc.poll() is None:
            stopped = stop_reason()
            if not stopped and watch is not None:
                stopped = watch()
            if stopped:
                _kill_tree(proc, job)
                break
            time.sleep(POLL_SECONDS)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, job)
            proc.wait(timeout=10)
        if job is not None:
            job.terminate()  # leftover grandchildren
        out.join(timeout=5)
        err.join(timeout=5)
        return RunResult(proc.returncode, bytes(out.data), bytes(err.data), stopped)
    finally:
        try:
            if job is not None or proc.poll() is None:
                _kill_tree(proc, job)
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            proc.wait(timeout=10)
        finally:
            try:
                for capture, pipe in ((out, proc.stdout), (err, proc.stderr)):
                    if capture is not None and capture.ident is not None:
                        capture.join(timeout=5)
                    else:
                        pipe.close()
            finally:
                if job is not None:
                    job.close()
