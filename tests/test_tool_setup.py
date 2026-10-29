import os
import sys

import pytest

from conftest import TOOLS, needs_ffmpeg
from mp4_audio_kit import __main__ as cli
from mp4_audio_kit import converter
from mp4_audio_kit.converter import Tools, tools_ready
from mp4_audio_kit.procguard import RunResult


@needs_ffmpeg
def test_real_tools_pass_startup_check():
    assert tools_ready(TOOLS)


@pytest.mark.parametrize("result", [RunResult(1, b"", b"private tool path"),
                                   RunResult(0, b"different program", b""),
                                   RunResult(0, b"ffprobe version fixture", b"", stopped="timeout")])
def test_setup_failure_stays_bounded_and_removes_temporary_folder(monkeypatch, result):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return result

    monkeypatch.setattr(converter, "run_bounded", run)
    assert not tools_ready(Tools(("encoder",), ("probe",)))
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["probe", "-version"]
    assert kwargs["max_processes"] == 1 and kwargs["timeout"] == 5
    assert not os.path.exists(kwargs["cwd"])


def test_startup_failure_reports_setup_before_creating_app_or_opening_browser(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mp4_audio_kit"])
    monkeypatch.setattr(cli, "find_tools", lambda: Tools(("shim",), ("shim",)))
    monkeypatch.setattr(cli, "tools_ready", lambda tools: False)
    monkeypatch.setattr(cli, "create_app", lambda **kwargs: pytest.fail("must not create runtime"))
    monkeypatch.setattr(cli.webbrowser, "open", lambda *args: pytest.fail("must not open browser"))
    with pytest.raises(SystemExit, match="actual FFmpeg bin folder"):
        cli.main()


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher process-slot regression")
def test_child_launching_shim_is_rejected_without_raising_process_cap():
    python = getattr(sys, "_base_executable", sys.executable)
    shim = (python, "-I", "-c", "import subprocess,sys; subprocess.run([sys.executable,'-I','-c',\"print('ffprobe version fixture')\"],check=True)")
    assert not tools_ready(Tools(shim, shim))
