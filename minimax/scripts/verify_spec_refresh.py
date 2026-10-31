from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from sdd_context.seekdb_store import SeekDBDocumentStore

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sdd-spec-refresh-") as directory:
        workspace = Path(directory)
        project = workspace / "project"
        current = project / "specs" / "task-filter.md"
        current.parent.mkdir(parents=True)
        shutil.copy(ROOT / "evidence/snapshots/before/task-filter.md", current)

        with SeekDBDocumentStore(workspace / "seekdb") as store:
            before = store.upsert_file(project, current)
            before_hits = store.search("status done filter")

            shutil.copy(ROOT / "evidence/snapshots/after/task-filter.md", current)
            after = store.upsert_file(project, current)
            after_hits = store.search("status done filter")
            history = store.import_history("specs/task-filter.md")

        report = {
            "before": {
                "version": before.version,
                "checksum": before.checksum,
                "top_excerpt": before_hits[0].excerpt,
            },
            "after": {
                "version": after.version,
                "checksum": after.checksum,
                "top_excerpt": after_hits[0].excerpt,
            },
            "import_events": len(history),
            "refreshed_content_used": after.version == 2
            and after.checksum != before.checksum
            and "status" in after_hits[0].excerpt,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report["refreshed_content_used"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
