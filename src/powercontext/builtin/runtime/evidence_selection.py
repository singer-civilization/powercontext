# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Query-ranked literal Memory spans, measured with their complete citations."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuild,
    PreparedContextFragment,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_TOKENS = re.compile(r"[^\W_]+", re.UNICODE)
_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+|\n+")  # noqa: RUF001 -- Unicode sentence punctuation.
_STOP = frozenset([
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "do",
    "does",
    "did",
    "has",
    "have",
    "had",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "from",
    "with",
    "and",
    "or",
    "as",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "their",
    "there",
    "which",
    "who",
    "when",
    "where",
    "can",
    "could",
    "may",
    "might",
    "will",
    "would",
    "should",
    "than",
    "then",
    "also",
    "both",
    "each",
    "all",
    "any",
    "such",
    "into",
    "through",
    "during",
    "between",
    "among",
    "we",
    "our",
    "they",
    "them",
    "he",
    "she",
    "i",
    "you",
    "your",
    "using",
    "used",
    "use",
    "study",
    "studies",
    "result",
    "results",
    "show",
    "shows",
    "showed",
    "shown",
    "find",
    "found",
    "suggest",
    "suggests",
])


def _stem(token: str) -> str:
    token = token.casefold()
    if len(token) > 5 and token.endswith("ies"):
        token = token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    for suffix, replacement in (
        ("ization", "ize"),
        ("ation", "ate"),
        ("ition", "it"),
        ("sion", "s"),
        ("ivity", "ive"),
        ("ability", "able"),
        ("ment", ""),
        ("ingly", ""),
        ("ing", ""),
        ("ed", ""),
    ):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            token = token[: -len(suffix)] + replacement
            break
    if len(token) > 4 and token.endswith("e"):
        token = token[:-1]
    return token


def _terms(text: str) -> Counter[str]:
    return Counter(_stem(match.group()) for match in _TOKENS.finditer(text) if match.group().casefold() not in _STOP)


@dataclass(frozen=True)
class _Passage:
    address: MemoryEntryAddress
    original: str
    start: int
    end: int
    terms: Counter[str]
    score: float = 0.0

    def fragment(self) -> PreparedContextFragment:
        return PreparedContextFragment(self.address, self.start, self.end, self.original[self.start : self.end])


def _sentences(text: str) -> list[tuple[int, int]]:
    boundaries = [0, *(match.end() for match in _BOUNDARY.finditer(text)), len(text)]
    spans = []
    for start, end in pairwise(boundaries):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            spans.append((start, end))
    return spans


def _passages(groups: Sequence[PreparedMemoryCandidates]) -> list[_Passage]:
    passages = []
    seen: set[tuple[str, str, int, str, str]] = set()
    for group in groups:
        if group.hits and group.memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        for hit in group.hits:
            if hit.memory_ref != group.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            address = MemoryEntryAddress(
                memory=ArtifactAddress(scope_id=group.scope_id, artifact=hit.memory_ref),
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            identity = (
                group.scope_id,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            if identity in seen:
                continue
            seen.add(identity)
            for start, end in _sentences(hit.text):
                passages.append(_Passage(address, hit.text, start, end, _terms(hit.text[start:end])))
    return passages


def _render(fragments: Sequence[PreparedContextFragment]) -> str:
    items = [
        {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,
                "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),
                "entry_id": fragment.address.entry_id,
                "entry_version_id": fragment.address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }
        for fragment in fragments
    ]
    encoded = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return f"{TRUST_POLICY}\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n{encoded}\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"


def _merge(selected: Sequence[_Passage], candidate: _Passage) -> list[_Passage]:
    combined = candidate
    remaining = []
    for passage in selected:
        if passage.address != candidate.address:
            remaining.append(passage)
            continue
        start = min(passage.start, combined.start)
        end = max(passage.end, combined.end)
        gap = passage.original[min(passage.end, combined.end) : max(passage.start, combined.start)]
        if (passage.start <= combined.end and combined.start <= passage.end) or not gap.strip():
            combined = _Passage(
                candidate.address, candidate.original, start, end, _terms(candidate.original[start:end])
            )
        else:
            remaining.append(passage)
    # Stable exact-address order prevents equal text in different scopes sharing identity.
    remaining.append(combined)
    return sorted(
        remaining,
        key=lambda item: (
            item.address.memory.scope_id,
            item.address.memory.artifact.artifact_id,
            item.address.memory.artifact.revision,
            item.address.entry_id,
            item.start,
        ),
    )


def _rank(passages: Sequence[_Passage], query: Counter[str]) -> list[_Passage]:
    frequencies = Counter(term for passage in passages for term in passage.terms)
    weights = {term: math.log(1 + (len(passages) + 1) / (frequencies[term] + 1)) for term in query}
    ranked = []
    for passage in passages:
        overlap = query.keys() & passage.terms.keys()
        if not overlap:
            continue
        score = sum(weights[term] * min(passage.terms[term], 2) ** 0.4 for term in overlap)
        score *= 1 + len(overlap) / max(1, len(query))
        score /= (max(10, sum(passage.terms.values())) / 25) ** 0.2
        ranked.append(_Passage(passage.address, passage.original, passage.start, passage.end, passage.terms, score))
    return sorted(ranked, key=lambda passage: -passage.score)


def _shorter_windows(passage: _Passage, query: Counter[str]) -> list[_Passage]:
    text = passage.original
    windows = []
    seen: set[tuple[int, int]] = set()
    for match in _TOKENS.finditer(text, passage.start, passage.end):
        if _stem(match.group()) not in query:
            continue
        for width in (180, 320, 500):
            start = max(passage.start, match.start() - width // 3)
            end = min(passage.end, start + width)
            start = max(passage.start, end - width)
            if start > passage.start:
                while start < match.start() and not text[start - 1].isspace():
                    start += 1
            if end < passage.end:
                while end > match.end() and not text[end].isspace():
                    end -= 1
            if start >= end or (start, end) in seen:
                continue
            seen.add((start, end))
            windows.append(_Passage(passage.address, text, start, end, _terms(text[start:end])))
    return windows


def select_evidence(request: PrepareContextRequest, groups: Sequence[PreparedMemoryCandidates]) -> PreparedContextBuild:
    """Choose complete relevant sentences first, then smaller literal windows."""

    query = _terms(request.query)
    passages = _rank(_passages(groups), query)
    selected: list[_Passage] = []
    current_bytes = 0
    for candidates in (
        passages,
        _rank([window for passage in passages for window in _shorter_windows(passage, query)], query),
    ):
        remaining = list(candidates)
        while remaining:
            best: tuple[float, int, list[_Passage]] | None = None
            for index, passage in enumerate(remaining):
                if any(
                    item.address == passage.address and item.start <= passage.start and item.end >= passage.end
                    for item in selected
                ):
                    continue
                merged = _merge(selected, passage)
                size = len(_render([item.fragment() for item in merged]).encode("utf-8"))
                if size > request.max_bytes:
                    continue
                priority = passage.score / max(40, size - current_bytes) ** (0.0 if not selected else 0.35)
                if best is None or priority > best[0]:
                    best = priority, index, merged
            if best is None:
                break
            _, index, selected = best
            del remaining[index]
            current_bytes = len(_render([item.fragment() for item in selected]).encode("utf-8"))
    fragments = tuple(item.fragment() for item in selected)
    if not fragments:
        return PreparedContextBuild(context=PreparedContext(status="empty", content=None, content_bytes=0), origins=())
    content = _render(fragments)
    return PreparedContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=len(content.encode("utf-8"))),
        origins=tuple(fragment.address for fragment in fragments),
        fragments=fragments,
    )
