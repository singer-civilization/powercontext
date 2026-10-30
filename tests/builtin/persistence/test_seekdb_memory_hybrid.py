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

"""Real seekdb hybrid candidates preserve the existing per-channel contract."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

from powercontext.builtin.artifacts.memory import EmbeddingProfile, MemoryEntryInput, MemorySearchRequest
from powercontext.builtin.inference import EmbeddingResult
from powercontext.builtin.persistence.memory_index import CompositeMemoryIndex
from powercontext.builtin.persistence.seekdb import SeekDBConfig
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryIndex
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts
from powercontext.builtin.tags import MemoryEntryTagTarget, TagFilter

_PROFILE = EmbeddingProfile(
    profile_id="native-hybrid-regression",
    model="deterministic",
    dimension=3,
    distance="l2",
    normalization="unit",
)


def _vector(number: int) -> tuple[float, ...]:
    angle = number / 70
    return (math.cos(angle), math.sin(angle), 0.0)


class _Embedding:
    profile = _PROFILE

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return EmbeddingResult(vectors=tuple(_vector(int(text.split()[0])) for text in texts))


def test_single_statement_preserves_two_query_channels_and_filters_before_top_k(short_tmp_path: Path) -> None:
    pytest.importorskip("pylibseekdb")

    async def scenario() -> None:
        async with open_builtin_contexts(
            BuiltinConfig(database=SeekDBConfig(path=short_tmp_path / "db")), embedding_model=_Embedding()
        ) as contexts:
            memory = (await contexts.get("project")).artifacts.memory
            first = await memory.remember(
                memory=None,
                entries=tuple(
                    MemoryEntryInput(kind="fact", text=f"{number} alpha record {number}") for number in range(50)
                ),
                mode="append",
            )
            assert first is not None
            head = await memory.remember(
                memory=first,
                entries=(MemoryEntryInput(kind="fact", text="60 beta appended record"),),
                mode="append",
            )
            assert head is not None
            # The two tagged records are outside the unfiltered vector top-k.
            tagged_ids = []
            for entry in await memory.entries(head):
                if entry.text.split()[0] in {"48", "49"}:
                    tagged_ids.append(entry.entry_id)
                    target = MemoryEntryTagTarget(artifact_id=head.artifact_id, entry_id=entry.entry_id)
                    current = await contexts.records.get_tags("project", target)
                    await contexts.records.replace_tags("project", target, ("selected",), expected_etag=current.etag)

            index = SeekDBMemoryIndex(_PROFILE)
            for query, number, tag_filter in (
                ("alpha", 10, None),
                ("beta", 60, None),
                ("unmatched", 30, None),
                ("alpha", 0, TagFilter(tags=("selected",))),
            ):
                request = MemorySearchRequest(
                    query=query,
                    analyzed_query=query,
                    memories=(head.as_ref(),),
                    mode="hybrid",
                    query_vector=_vector(number),
                    embedding_profile=_PROFILE,
                    candidate_limit=5,
                    tag_filter=tag_filter,
                )
                async with contexts.database.transaction() as connection:
                    recorded: list[tuple[str, Any]] = []

                    def capture(
                        _connection, _cursor, statement, parameters, _context, _executemany, recorded=recorded
                    ) -> None:
                        if "AGAINST" in statement.upper() or "l2_distance" in statement.lower():
                            recorded.append((statement, parameters))

                    event.listen(connection.sync_connection, "before_cursor_execute", capture)
                    try:
                        candidate = await index.search(connection, "project", request)
                    finally:
                        event.remove(connection.sync_connection, "before_cursor_execute", capture)
                    assert len(recorded) == 1
                    reference = await CompositeMemoryIndex.search(index, connection, "project", request)
                    assert candidate == reference
                    assert await index.search(connection, "project", request) == candidate
                    if tag_filter is not None:
                        assert {hit.entry_id for hit in candidate.vector} == set(tagged_ids)
                        assert {hit.entry_id for hit in candidate.fts} == set(tagged_ids)
                    statement, parameters = recorded[0]
                    plan = "\n".join(
                        str(row[0])
                        for row in (await connection.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
                    )
                    assert "TEXT RETRIEVAL SCAN" in plan
                    assert "VECTOR INDEX" in plan
                    assert all(hit.memory_ref == head.as_ref() for hit in candidate.fts + candidate.vector)

    asyncio.run(scenario())
