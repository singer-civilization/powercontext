from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Self

from .models import Document, SearchHit

TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def python_symbols(content: str) -> str:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return ""
    names = [
        node.name for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return ",".join(sorted(set(names)))


def infer_kind(path: Path) -> str:
    normalized = path.as_posix().lower()
    if "/spec" in f"/{normalized}" or path.suffix.lower() in {".md", ".rst"}:
        return "spec"
    if "/test" in f"/{normalized}" or path.name.startswith("test_"):
        return "test"
    return "code"


class SeekDBDocumentStore:
    """Small, explicit document index using the official embedded seekdb binding."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._instance: Any | None = None
        self._connection: Any | None = None

    def open(self) -> SeekDBDocumentStore:
        try:
            import pylibseekdb
            import pymysql
        except ImportError as exc:  # pragma: no cover - exercised by clean-install smoke test
            raise RuntimeError("Install the project first: python -m pip install '.[dev]'") from exc

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._instance = pylibseekdb.open(str(self.database_path))
        options = self._instance.connection_options()
        self._connection = pymysql.connect(
            database="test",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            **options,
        )
        self._ensure_schema()
        return self

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._instance is not None:
            self._instance.close()
            self._instance = None

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def connection(self) -> Any:
        if self._connection is None:
            raise RuntimeError("seekdb store is not open")
        return self._connection

    def _ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS sdd_documents (
                doc_id VARCHAR(64) PRIMARY KEY,
                kind VARCHAR(32) NOT NULL,
                path VARCHAR(512) NOT NULL,
                symbol VARCHAR(1024) NOT NULL,
                content LONGTEXT NOT NULL,
                checksum CHAR(64) NOT NULL,
                version INT NOT NULL,
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                INDEX idx_sdd_kind_path (kind, path(191))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sdd_import_events (
                event_id BIGINT AUTO_INCREMENT PRIMARY KEY,
                path VARCHAR(512) NOT NULL,
                old_checksum CHAR(64) NULL,
                new_checksum CHAR(64) NOT NULL,
                version INT NOT NULL,
                imported_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                INDEX idx_import_path (path(191))
            )
            """,
        ]
        with self.connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    def upsert_file(self, project_root: Path, file_path: Path) -> Document:
        resolved_root = project_root.resolve()
        resolved_file = file_path.resolve()
        try:
            relative = resolved_file.relative_to(resolved_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Refusing to ingest a file outside project root: {file_path}") from exc
        content = resolved_file.read_text(encoding="utf-8")
        checksum = sha256_text(content)
        symbol = python_symbols(content) if resolved_file.suffix == ".py" else ""
        kind = infer_kind(Path(relative))

        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT checksum, version FROM sdd_documents WHERE path = %s ORDER BY version DESC LIMIT 1",
                (relative,),
            )
            current = cursor.fetchone()
            if current and current["checksum"] == checksum:
                return Document(kind, relative, content, symbol, checksum, int(current["version"]))
            version = int(current["version"]) + 1 if current else 1
            old_checksum = current["checksum"] if current else None
            cursor.execute("DELETE FROM sdd_documents WHERE path = %s", (relative,))
            doc_id = sha256_text(f"{relative}\0{checksum}")
            cursor.execute(
                """
                INSERT INTO sdd_documents (doc_id, kind, path, symbol, content, checksum, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (doc_id, kind, relative, symbol, content, checksum, version),
            )
            cursor.execute(
                """
                INSERT INTO sdd_import_events (path, old_checksum, new_checksum, version)
                VALUES (%s, %s, %s, %s)
                """,
                (relative, old_checksum, checksum, version),
            )
        return Document(kind, relative, content, symbol, checksum, version)

    def ingest(self, project_root: Path, paths: Iterable[Path]) -> list[Document]:
        return [self.upsert_file(project_root, path) for path in paths]

    def search(self, query: str, *, limit: int = 8) -> list[SearchHit]:
        tokens = list(dict.fromkeys(TOKEN_PATTERN.findall(query)))[:12]
        if not tokens:
            tokens = [query.strip()]
        clauses: list[str] = []
        params: list[str | int] = []
        for token in tokens:
            wildcard = f"%{token}%"
            clauses.append("(content LIKE %s OR path LIKE %s OR symbol LIKE %s)")
            params.extend((wildcard, wildcard, wildcard))
        sql = (
            "SELECT kind, path, symbol, content, checksum, version FROM sdd_documents WHERE "
            + " OR ".join(clauses)
            + " LIMIT %s"
        )
        params.append(max(limit * 4, 20))
        with self.connection.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()

        hits: list[SearchHit] = []
        lowered_tokens = [token.casefold() for token in tokens]
        for row in rows:
            content = str(row["content"])
            searchable = f"{row['path']} {row['symbol']} {content}".casefold()
            score = sum(searchable.count(token) for token in lowered_tokens)
            line, excerpt = _best_excerpt(content, lowered_tokens)
            hits.append(
                SearchHit(
                    kind=str(row["kind"]),
                    path=str(row["path"]),
                    symbol=str(row["symbol"]),
                    checksum=str(row["checksum"]),
                    version=int(row["version"]),
                    score=float(score),
                    excerpt=excerpt,
                    line=line,
                )
            )
        return sorted(hits, key=lambda hit: (-hit.score, hit.kind, hit.path))[:limit]

    def import_history(self, path: str) -> list[dict[str, Any]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT path, old_checksum, new_checksum, version, imported_at
                FROM sdd_import_events WHERE path = %s ORDER BY event_id
                """,
                (path,),
            )
            return list(cursor.fetchall())


def _best_excerpt(content: str, tokens: list[str]) -> tuple[int, str]:
    lines = content.splitlines() or [""]
    ranked = []
    for index, line in enumerate(lines, start=1):
        folded = line.casefold()
        ranked.append((sum(folded.count(token) for token in tokens), index, line.strip()))
    _, line_number, line = max(ranked, key=lambda item: (item[0], -item[1]))
    return line_number, line[:300]
