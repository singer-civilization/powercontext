from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "sample_project" / "tests" / "test_taskboard.py"


def run_snapshot(name: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix=f"sdd-{name}-") as directory:
        target = Path(directory)
        shutil.copy(ROOT / "evidence" / "snapshots" / name / "taskboard.py", target / "taskboard.py")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(target)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(TEST)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )


def main() -> int:
    before = run_snapshot("before")
    after = run_snapshot("after")
    print("=== BEFORE (expected failure) ===")
    print(before.stdout, end="")
    print(before.stderr, end="")
    print("=== AFTER (expected pass) ===")
    print(after.stdout, end="")
    print(after.stderr, end="")
    if before.returncode == 0:
        print("ERROR: before snapshot unexpectedly passed", file=sys.stderr)
        return 1
    if after.returncode != 0:
        print("ERROR: after snapshot failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
