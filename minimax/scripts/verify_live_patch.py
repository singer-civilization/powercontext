from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "sample_project" / "tests" / "test_taskboard.py"
PATCH = ROOT / "evidence" / "live" / "minimax.patch"


def apply_patch(patch: Path, workdir: Path) -> subprocess.CompletedProcess[str]:
    for strip in ("-p1", "-p0"):
        result = subprocess.run(
            ["git", "apply", "--recount", strip, str(patch)],
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result
    return result


def main() -> int:
    if not PATCH.exists():
        print(f"missing {PATCH}; run the README generate command first", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="sdd-live-") as directory:
        workdir = Path(directory)
        source = workdir / "sample_project" / "src"
        source.mkdir(parents=True)
        shutil.copy(ROOT / "evidence" / "snapshots" / "before" / "taskboard.py", source / "taskboard.py")

        applied = apply_patch(PATCH, workdir)
        if applied.returncode != 0:
            print(applied.stdout, end="")
            print(applied.stderr, end="", file=sys.stderr)
            print("ERROR: the live patch did not apply to the before snapshot", file=sys.stderr)
            return 1

        env = os.environ.copy()
        env["PYTHONPATH"] = str(source)
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(TEST)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        print(tests.stdout, end="")
        print(tests.stderr, end="")
        if tests.returncode != 0:
            print("ERROR: the patched code still fails the target tests", file=sys.stderr)
            return 1

        print("live patch applied cleanly and the target tests pass")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
