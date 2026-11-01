"""Audit every nested lock file and reject omitted or skipped distributions."""
import json
from pathlib import Path
import re
import subprocess
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def main():
    files, expected = [], {}

    def collect(path):
        path = path.resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError("Dependency includes must remain inside this repository")
        if path in files:
            return
        files.append(path)
        text = path.read_text("utf-8").replace("\\\n", "")
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-r "):
                collect(path.parent / line[3:].strip())
                continue
            declaration, hashes = re.subn(r"\s+--hash=sha256:[a-f0-9]{64}", "", line)
            requirement = Requirement(declaration)
            specs = list(requirement.specifier)
            if (not hashes or requirement.url or requirement.extras or len(specs) != 1
                    or specs[0].operator != "==" or "*" in specs[0].version):
                raise ValueError("Every audited dependency must be an exact, hashed wheel pin")
            if requirement.marker and not requirement.marker.evaluate():
                continue
            name, version = canonicalize_name(requirement.name), specs[0].version
            if name in expected and expected[name] != version:
                raise ValueError("Conflicting locked dependency versions")
            expected[name] = version

    for name in ("requirements-dev.txt", "requirements-tools.txt"):
        collect(ROOT / name)
    output = ROOT / "work" / "installed-validation" / "dependency-audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "pip_audit", "--strict", "--disable-pip", "--require-hashes",
               "--format", "json", "--output", str(output)]
    # --disable-pip does not follow nested includes: supply each collected file explicitly.
    for path in files:
        command.extend(("-r", str(path)))
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180)
    if process.returncode:
        raise RuntimeError("Locked dependency audit failed:\n" + process.stdout + process.stderr)
    report = json.loads(output.read_text("utf-8"))
    actual = {}
    for item in report["dependencies"]:
        if item.get("skip_reason") or item.get("vulns") or not item.get("version"):
            raise AssertionError("Dependency audit contains a skipped or vulnerable distribution")
        actual[canonicalize_name(item["name"])] = item["version"]
    if actual != expected:
        raise AssertionError("Dependency audit omitted or changed locked distributions: "
                             + repr(sorted(set(expected.items()) ^ set(actual.items()))))
    print(json.dumps({"distributions": len(actual), "lock_files": len(files),
                      "known_vulnerabilities": 0, "skipped": 0}))


if __name__ == "__main__":
    main()
