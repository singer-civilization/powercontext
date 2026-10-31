from __future__ import annotations

import json
from pathlib import Path

import pytest

from researchvault.cli import main
from researchvault.store import VaultStore

DB_NOTES = """# 数据库索引笔记

B+ 树索引把数据放在叶子层，二级索引查询需要回表。
覆盖索引避免回表，EXPLAIN 显示 Using index。
"""

PAPER = """# Attention 论文摘录

Attention(Q, K, V) = softmax(QK^T / sqrt(d_k)) V
多头注意力让模型关注不同位置。
"""


@pytest.fixture()
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "vault-home"
    monkeypatch.setenv("RESEARCHVAULT_HOME", str(home))
    return home


@pytest.fixture()
def notes_dir(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "db.md").write_text(DB_NOTES, encoding="utf-8")
    (root / "paper.md").write_text(PAPER, encoding="utf-8")
    nested = root / "sub"
    nested.mkdir()
    (nested / "todo.txt").write_text("复习覆盖索引\n", encoding="utf-8")
    return root


def test_add_indexes_files_with_lines(vault: Path, notes_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(notes_dir)
    with VaultStore() as store:
        result = store.upsert_file(notes_dir / "db.md", base_dir=notes_dir)
        assert result.status == "imported"
        stats = store.stats()
        assert stats.documents == 1
        assert stats.chunks >= 1
        assert stats.last_imported_at != "-"
        hits = store.search("覆盖索引")
        assert hits
        assert hits[0].file == "db.md"
        # 出处行号要真的指向包含关键词的那一行
        source = (notes_dir / "db.md").read_text(encoding="utf-8").splitlines()
        assert "覆盖索引" in source[hits[0].line - 1]


def test_search_ranks_by_score(vault: Path, notes_dir: Path) -> None:
    with VaultStore() as store:
        store.upsert_file(notes_dir / "db.md", base_dir=notes_dir)
        store.upsert_file(notes_dir / "paper.md", base_dir=notes_dir)
        hits = store.search("索引")
        assert all(hit.file.endswith("db.md") for hit in hits)
        assert hits[0].score >= hits[-1].score
        assert all(hit.excerpt for hit in hits)


def test_reimport_does_not_duplicate(vault: Path, notes_dir: Path) -> None:
    with VaultStore() as store:
        first = store.upsert_file(notes_dir / "db.md", base_dir=notes_dir)
        assert first.status == "imported"
        repeat = store.upsert_file(notes_dir / "db.md", base_dir=notes_dir)
        assert repeat.status == "unchanged"
        stats = store.stats()
        assert stats.documents == 1
        chunks_after_repeat = stats.chunks

        (notes_dir / "db.md").write_text(DB_NOTES + "\n新增：前缀索引。\n", encoding="utf-8")
        edited = store.upsert_file(notes_dir / "db.md", base_dir=notes_dir)
        assert edited.status == "imported"
        stats = store.stats()
        assert stats.documents == 1
        assert stats.chunks >= chunks_after_repeat


def test_unknown_query_returns_empty(vault: Path, notes_dir: Path) -> None:
    with VaultStore() as store:
        store.upsert_file(notes_dir / "db.md", base_dir=notes_dir)
        assert store.search("量子隧穿效应") == []
        assert store.search("") == []


def test_cli_add_search_stats_json(
    vault: Path, notes_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(notes_dir)
    assert main(["add", "."]) == 0
    out = capsys.readouterr().out
    assert "db.md" in out and "paper.md" in out and "sub/todo.txt" in out

    assert main(["search", "回表", "--json", "--limit", "3"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload and payload[0]["file"] == "db.md" and payload[0]["line"] > 0

    assert main(["stats", "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["documents"] == 3
    assert stats["chunks"] >= 3
    assert stats["last_imported_at"] != "-"

    assert main(["search", "不存在xyzwdq", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
