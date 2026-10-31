from pathlib import Path

from sdd_context.seekdb_store import SeekDBDocumentStore


def test_seekdb_refresh_replaces_old_spec_and_increments_version(tmp_path: Path) -> None:
    project = tmp_path / "project"
    spec = project / "specs" / "feature.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("v1: return all tasks\n", encoding="utf-8")
    database = tmp_path / "seekdb"

    with SeekDBDocumentStore(database) as store:
        first = store.upsert_file(project, spec)
        spec.write_text("v2: filter done tasks by status\n", encoding="utf-8")
        second = store.upsert_file(project, spec)
        hits = store.search("done status")
        history = store.import_history("specs/feature.md")

    assert first.version == 1
    assert second.version == 2
    assert hits[0].version == 2
    assert "v2" in hits[0].excerpt
    assert len(history) == 2
    assert history[1]["old_checksum"] == first.checksum
