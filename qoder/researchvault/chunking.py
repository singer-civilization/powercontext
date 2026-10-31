from __future__ import annotations

from dataclasses import dataclass

MAX_LINES = 40
MAX_CHARS = 4000


@dataclass(frozen=True)
class Chunk:
    index: int
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    content: str


def split_chunks(
    text: str,
    *,
    max_lines: int = MAX_LINES,
    max_chars: int = MAX_CHARS,
) -> list[Chunk]:
    """Cut a document into line-window chunks with 1-based line ranges.

    A chunk holds at most ``max_lines`` lines and at most ``max_chars``
    characters; a line longer than the budget still forms its own chunk.
    """
    lines = text.splitlines()
    chunks: list[Chunk] = []
    start = 1
    current: list[str] = []
    current_chars = 0

    def flush(end_line: int) -> None:
        chunks.append(Chunk(len(chunks), start, end_line, "\n".join(current)))

    for lineno, line in enumerate(lines, start=1):
        if current and (len(current) >= max_lines or current_chars + len(line) + 1 > max_chars):
            flush(lineno - 1)
            start = lineno
            current = []
            current_chars = 0
        current.append(line)
        current_chars += len(line) + 1
    if current:
        flush(len(lines))
    return chunks
