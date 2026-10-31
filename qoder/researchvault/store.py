from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .chunking import split_chunks
from .config import engine_dir, home_dir

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
CJK_PATTERN = re.compile(r"^[\u4e00-\u9fff]+$")
SUPPORTED_SUFFIXES = {".md", ".txt"}
MAX_TOKENS = 12


def tokenize(query: str) -> list[str]:
    """Split a query into LIKE/scoring tokens.

    ASCII words pass through. A CJK run of 3+ chars also yields its
    overlapping bigrams, because seekdb LIKE is substring-only and a whole
    run like 多头注意力 would miss the sentence 关于多头注意力的综述.
    """
    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer(query):
        token = match.group()
        tokens.append(token)
        if len(token) >= 3 and CJK_PATTERN.match(token):
            tokens.extend(token[i : i + 2] for i in range(len(token) - 1))
    return list(dict.fromkeys(tokens))[:MAX_TOKENS]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def quote(value: str) -> str:
    """Render a Python string as a seekdb/MySQL single-quoted literal."""
    cleaned = value.replace("\x00", "")
    escaped = cleaned.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


@dataclass
class ImportResult:
    rel_path: str
    chunks: int
    status: str  # imported | unchanged


@dataclass
class SearchHit:
    file: str
    line: int
    score: float
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Stats:
    documents: int
    chunks: int
    last_imported_at: str


def iter_source_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix.lower() in SUPPORTED_SUFFIXES else []
    found: list[Path] = []
    for path in sorted(target.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            found.append(path)
    return found


def display_rel(target: Path, base: Path) -> str:
    try:
        return os.path.relpath(target, base).replace(os.sep, "/")
    except ValueError:
        return target.as_posix()


class VaultStore:
    """Embedded seekdb document store. Standard library + pylibseekdb only.

    pylibseekdb's Cursor.execute() takes a fully rendered SQL string (no
    bound parameters), so every dynamic value goes through ``quote()``.
    """

    def __init__(self, vault_home: Path | None = None) -> None:
        self.home = vault_home or home_dir()
        self._instance: Any | None = None
        self._connection: Any | None = None

    def open(self) -> "VaultStore":
        import pylibseekdb

        self.home.mkdir(parents=True, exist_ok=True)
        db_dir = engine_dir(self.home)
        self._instance = pylibseekdb.open(str(db_dir))
        self._connection = self._instance.connect(autocommit=True)
        self._ensure_schema()
        return self

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._instance is not None:
            self._instance.close()
            self._instance = None

    def __enter__(self) -> "VaultStore":
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def connection(self) -> Any:
        if self._connection is None:
            raise RuntimeError("VaultStore is not open")
        return self._connection

    def _query(self, sql: str) -> list[tuple]:
        cursor = self.connection.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall() or []
        cursor.close()
        return list(rows)

    def _execute(self, sql: str) -> None:
        cursor = self.connection.cursor()
        cursor.execute(sql)
        cursor.close()

    def _ensure_schema(self) -> None:
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS rv_documents (
                doc_id VARCHAR(64) PRIMARY KEY,
                abs_path VARCHAR(1024) NOT NULL,
                rel_path VARCHAR(512) NOT NULL,
                checksum CHAR(64) NOT NULL,
                chunk_count INT NOT NULL,
                char_count INT NOT NULL,
                version INT NOT NULL,
                imported_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS rv_chunks (
                chunk_id VARCHAR(64) PRIMARY KEY,
                doc_id VARCHAR(64) NOT NULL,
                rel_path VARCHAR(512) NOT NULL,
                start_line INT NOT NULL,
                end_line INT NOT NULL,
                checksum CHAR(64) NOT NULL,
                content LONGTEXT NOT NULL,
                INDEX idx_rv_chunks_doc (doc_id)
            )
            """
        )

    def upsert_file(self, file_path: Path, base_dir: Path | None = None) -> ImportResult:
        base = (base_dir or Path.cwd()).resolve()
        resolved = file_path.resolve()
        rel = display_rel(resolved, base)
        text = resolved.read_text(encoding="utf-8", errors="replace")
        checksum = sha256_text(text)
        doc_id = sha256_text(str(resolved))

        rows = self._query(f"SELECT checksum FROM rv_documents WHERE doc_id = {quote(doc_id)}")
        if rows and rows[0][0] == checksum:
            return ImportResult(rel, 0, "unchanged")

        chunks = split_chunks(text)
        self._execute(f"DELETE FROM rv_chunks WHERE doc_id = {quote(doc_id)}")
        for chunk in chunks:
            chunk_id = sha256_text(f"{doc_id}\0{chunk.start_line}\0{chunk.end_line}")
            self._execute(
                "INSERT INTO rv_chunks (chunk_id, doc_id, rel_path, start_line, end_line, checksum, content) "
                f"VALUES ({quote(chunk_id)}, {quote(doc_id)}, {quote(rel)}, "
                f"{chunk.start_line}, {chunk.end_line}, {quote(checksum)}, {quote(chunk.content)})"
            )

        version_rows = self._query(f"SELECT version FROM rv_documents WHERE doc_id = {quote(doc_id)}")
        version = int(version_rows[0][0]) + 1 if version_rows else 1
        if version_rows:
            self._execute(
                "UPDATE rv_documents SET abs_path = " + quote(str(resolved))
                + f", rel_path = {quote(rel)}, checksum = {quote(checksum)}, "
                f"chunk_count = {len(chunks)}, char_count = {len(text)}, version = {version}, "
                f"updated_at = CURRENT_TIMESTAMP(6) WHERE doc_id = {quote(doc_id)}"
            )
        else:
            self._execute(
                "INSERT INTO rv_documents (doc_id, abs_path, rel_path, checksum, chunk_count, char_count, version) "
                f"VALUES ({quote(doc_id)}, {quote(str(resolved))}, {quote(rel)}, {quote(checksum)}, "
                f"{len(chunks)}, {len(text)}, {version})"
            )
        return ImportResult(rel, len(chunks), "imported")

    def search(self, query: str, limit: int = 8) -> list[SearchHit]:
        tokens = list(dict.fromkeys(TOKEN_PATTERN.findall(query)))[:12]
        if not tokens and query.strip():
            tokens = [query.strip()]
        if not tokens:
            return []

        conditions = " OR ".join(f"content LIKE {quote('%' + t + '%')}" for t in tokens)
        rows = self._query(
            "SELECT rel_path, start_line, content FROM rv_chunks WHERE " + conditions + " LIMIT 500"
        )

        folded = [t.casefold() for t in tokens]
        whole = query.strip().casefold()
        scored: list[SearchHit] = []
        for rel_path, start_line, content in rows:
            text = str(content)
            lowered = text.casefold()
            occurrences = sum(lowered.count(token) for token in folded)
            if occurrences == 0:
                continue
            score = float(occurrences)
            if whole and whole in lowered:
                score += 4.0
            line_no, excerpt = _best_line(text, folded, int(start_line))
            scored.append(SearchHit(str(rel_path), line_no, score, excerpt))

        scored.sort(key=lambda hit: (-hit.score, hit.file, hit.line))
        return scored[: max(limit, 1)]

    def stats(self) -> Stats:
        docs = self._query("SELECT COUNT(*) FROM rv_documents")[0][0]
        chunks = self._query("SELECT COUNT(*) FROM rv_chunks")[0][0]
        latest = self._query(
            "SELECT MAX(updated_at) FROM rv_documents"
        )[0][0]
        stamp = latest.strftime("%Y-%m-%d %H:%M:%S") if latest is not None else "-"
        return Stats(int(docs), int(chunks), stamp)


def _best_line(content: str, folded_tokens: list[str], start_line: int) -> tuple[int, str]:
    best_count = -1
    best_offset = 0
    lines = content.splitlines() or [""]
    for offset, line in enumerate(lines):
        count = sum(line.casefold().count(token) for token in folded_tokens)
        if count > best_count:
            best_count = count
            best_offset = offset
    chosen = lines[best_offset].strip()
    if len(chosen) > 160:
        chosen = chosen[:160] + "…"
    return start_line + best_offset, chosen
