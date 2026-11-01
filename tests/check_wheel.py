"""Build and validate an installed wheel outside this checkout with fake processing."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import venv

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd, environment):
    result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=180,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Validation command failed:\n" + result.stdout[-5000:] + result.stderr[-5000:])
    return result.stdout


def main():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["name"]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                   "PIP_DISABLE_PIP_VERSION_CHECK": "1", "YFK_REAL_NETWORK": "0"}
    output = ROOT / "work" / "installed-validation"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kit-install-") as scratch:
        # Only this newly owned synthetic root is cleaned up by TemporaryDirectory.
        base = Path(scratch).resolve()
        assert base.parent == Path(tempfile.gettempdir()).resolve()
        launch = base / "launch space résumé 空间"
        launch.mkdir()
        wheels = base / "wheels"
        wheels.mkdir()
        run([sys.executable, "-X", "utf8", "-m", "pip", "--isolated", "wheel",
             "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), str(ROOT)],
            cwd=launch, environment=environment)
        built = list(wheels.glob("*.whl"))
        assert len(built) == 1, "Expected exactly one project wheel"
        wheel = built[0]
        target = base / "environment"
        venv.EnvBuilder(with_pip=True).create(target)
        python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        for requirements in ("requirements-bootstrap.txt", "requirements-dev.txt"):
            run([str(python), "-X", "utf8", "-m", "pip", "--isolated", "install",
                 "--index-url", "https://pypi.org/simple", "--require-hashes",
                 "--only-binary=:all:", "-r", str(ROOT / requirements)],
                cwd=launch, environment=environment)
        run([str(python), "-X", "utf8", "-m", "pip", "--isolated", "install",
             "--no-deps", "--no-index", str(wheel)], cwd=launch, environment=environment)
        run([str(python), "-m", "pip", "check"], cwd=launch, environment=environment)
        smoke = launch / "installed_smoke.py"
        smoke.write_bytes((ROOT / "tests" / "installed_smoke.py").read_bytes())
        receipt = json.loads(run([str(python), "-I", "-X", "utf8", str(smoke), str(ROOT)],
                                 cwd=launch, environment=environment))
        assert receipt["installed"] is True and len(receipt["checks"]) >= 5
        package = project.replace("-", "_")
        run([str(python), "-I", "-m", package, "--help"], cwd=launch, environment=environment)
        console = target / ("Scripts/" + project + ".exe" if os.name == "nt" else "bin/" + project)
        assert console.is_file(), "Installed console entry point is missing"
        run([str(console), "--help"], cwd=launch, environment=environment)
        receipt.update({"project": project, "wheel": wheel.name,
                        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                        "launch_path_has_spaces_and_non_ascii": True,
                        "module_and_console_help": True})
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

