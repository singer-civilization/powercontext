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

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft


@pytest.mark.parametrize("available", [False, True])
def test_native_state_write_failure_restarts_and_replays_without_extra_revisions(
    tmp_path: Path, available: bool
) -> None:
    async def scenario() -> None:
        database = tmp_path / "lifecycle.db"
        settings = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
        async with open_builtin_runtime(settings) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Lifecycle", summary="Recovery", idempotency_key="retry")
            )
            scope_id = scope.scope_id
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            receipt = await runtime.sources.for_scope(scope_id).capture(
                CaptureSource(source_id="root", content="resilience sensor evidence", metadata={})
            )
            await lifecycle.register_source("root", receipt.source_ref, revision=3)
            memory = runtime.memory.for_scope(scope_id)
            citations = []
            for number in range(2):
                created = await memory.remember(
                    RememberMemoryRequest(
                        entries=(MemoryEntryInput(kind="fact", text=f"resilience sensor number {number}"),)
                    )
                )
                assert created.entry is not None
                citations.append(created.entry.citation)
                await lifecycle.register_memory(
                    f"memory-{number}",
                    created.entry.citation,
                    parents=("root" if number == 0 else "memory-0",),
                    policy="all",
                )
            topic = await runtime.records.for_scope(scope_id).create_artifact(
                "topic-memory",
                ArtifactWrite(
                    content={
                        "title": "resilience sensor status",
                        "summary": "resilience sensor supported status",
                        "detail": "resilience sensor evidence",
                    }
                ),
            )
            topic_ref = ArtifactRef(family="topic-memory", artifact_id=topic.artifact_id, revision=topic.revision)
            await lifecycle.register_topic("topic", topic_ref, parents=("memory-1",), policy="all")
            if available:
                await lifecycle.apply(
                    SourceLifecycleEvent(node_id="root", revision=3, sequence=1, available=False, reason="withdrawal")
                )
            before = (await memory.list(include_inactive=True)).memory_ref
            assert before is not None
            event = SourceLifecycleEvent(
                node_id="root",
                revision=3,
                sequence=2 if available else 1,
                available=available,
                reason="restore" if available else "withdrawal",
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER interrupt_memory BEFORE INSERT ON pc_artifacts WHEN NEW.family = 'memory' BEGIN SELECT RAISE(ABORT, 'controlled lifecycle write failure'); END"
                )
            with pytest.raises(IntegrityError, match="controlled lifecycle write failure"):
                await lifecycle.apply(event)
            assert (await memory.list(include_inactive=True)).memory_ref == before

        with sqlite3.connect(database) as connection:
            connection.execute("DROP TRIGGER interrupt_memory")
        async with open_builtin_runtime(settings) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope_id)
            memory = runtime.memory.for_scope(scope_id)
            assert await lifecycle.apply(event) == (["memory-0", "memory-1", "root", "topic"] if available else [])
            page = await memory.list(include_inactive=True)
            repaired = page.memory_ref
            assert repaired is not None and repaired.revision == before.revision + 1
            assert all((entry.state == "active") == available for entry in page.entries)
            assert (
                bool((await memory.search(SearchMemoryRequest(query="resilience sensor", mode="fts"))).hits)
                == available
            )
            assert (
                bool(
                    (
                        await runtime.topic_memory.for_scope(scope_id).search(
                            SearchTopicMemoryRequest(query="resilience sensor")
                        )
                    ).hits
                )
                == available
            )
            prepared = await runtime.context.for_scope(scope_id).prepare(
                PrepareContextRequest(query="resilience sensor")
            )
            assert bool(prepared.content) == available
            for number, citation in enumerate(citations):
                original = await memory.get(GetMemoryEntryRequest(citation=citation))
                assert original.entry.text == f"resilience sensor number {number}"
                assert await lifecycle.resolve(f"memory-{number}", purpose="audit") == citation
                if available:
                    assert await lifecycle.resolve(f"memory-{number}", purpose="generation") == citation
                else:
                    with pytest.raises(SourceLifecycleError):
                        await lifecycle.resolve(f"memory-{number}", purpose="generation")
            assert await lifecycle.resolve("topic", purpose="audit") == topic_ref
            await lifecycle.apply(event)
            assert (await memory.list(include_inactive=True)).memory_ref == repaired

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": True},
        {"revision": "3"},
        {"sequence": 1.0},
        {"sequence": False},
        {"available": 1},
        {"reason": "  "},
        {"node_id": ""},
        {"extra": True},
    ],
)
def test_lifecycle_events_require_strict_fields(changes: dict[str, object]) -> None:
    values = {"node_id": "root", "revision": 3, "sequence": 1, "available": False, "reason": "withdrawal", **changes}
    with pytest.raises(ValidationError):
        SourceLifecycleEvent.model_validate(values)


def test_node_ids_and_events_are_isolated_by_scope() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig()) as runtime:
            assert runtime.scopes is not None
            scopes = []
            for key in ("left", "right"):
                scope = await runtime.scopes.create(
                    ScopeDraft(title=key, summary="Scope isolation", idempotency_key=key)
                )
                scopes.append(scope.scope_id)
                receipt = await runtime.sources.for_scope(scope.scope_id).capture(
                    CaptureSource(source_id="root", content="scope specific sensor", metadata={})
                )
                lifecycle = runtime.lifecycle.for_scope(scope.scope_id)
                await lifecycle.register_source("root", receipt.source_ref, revision=1)
                created = await runtime.memory.for_scope(scope.scope_id).remember(
                    RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="scope specific sensor"),))
                )
                assert created.entry is not None
                await lifecycle.register_memory("memory", created.entry.citation, parents=("root",), policy="all")
            left, right = scopes
            event = SourceLifecycleEvent(
                node_id="root", revision=1, sequence=1, available=False, reason="left withdrawal"
            )
            assert await runtime.lifecycle.for_scope(left).apply(event) == []
            assert not (
                await runtime.memory.for_scope(left).search(SearchMemoryRequest(query="specific sensor", mode="fts"))
            ).hits
            assert (
                await runtime.memory.for_scope(right).search(SearchMemoryRequest(query="specific sensor", mode="fts"))
            ).hits
            assert await runtime.lifecycle.for_scope(right).resolve("memory", purpose="generation")

    asyncio.run(scenario())
