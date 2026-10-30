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

"""Persistent evidence eligibility, retaining exact historical references."""

# Lifecycle validation failures retain their specific public contract explanation.
# ruff: noqa: TRY003

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.source_lifecycle import load_lifecycle, save_lifecycle
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """An invalid graph, event, or generation request."""


class SourceLifecycleEvent(BaseModel):
    """One strictly typed, replayable Source availability transition."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def validate_text(cls, value: str) -> str:
        _require_text(value)
        return value


class _Node(BaseModel):
    kind: Literal["source", "memory", "topic"]
    reference: dict[str, JsonValue]
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"
    revision: int | None = None
    available: bool = True
    event: SourceLifecycleEvent | None = None


def _require_text(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("node identities and reasons must be nonempty strings")


def _evaluate(nodes: dict[str, _Node]) -> None:
    evaluated: set[str] = set()

    def visit(node_id: str) -> bool:
        node = nodes[node_id]
        if node_id not in evaluated:
            if node.kind != "source":
                support = [visit(parent) for parent in node.parents]
                node.available = all(support) if node.policy == "all" else any(support)
            evaluated.add(node_id)
        return node.available

    for node_id in nodes:
        visit(node_id)


class ScopedSourceLifecycle:
    """Connect one immutable dependency graph to native Memory state writes."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase | None, scope_id: str) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = validate_scope_id(scope_id)

    def _db(self) -> AsyncDatabase:
        if self._database is None:
            raise SourceLifecycleError("lifecycle requires a relational Runtime")
        return self._database

    async def _load(self) -> dict[str, _Node]:
        async with self._db().transaction() as connection:
            return {
                key: _Node.model_validate(value)
                for key, value in (await load_lifecycle(connection, self.scope_id)).items()
            }

    async def _save(self, nodes: dict[str, _Node]) -> None:
        async with self._db().transaction() as connection:
            await save_lifecycle(
                connection, self.scope_id, {key: node.model_dump(mode="json") for key, node in nodes.items()}
            )

    async def register_source(self, node_id: str, reference: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("Source revision must be a positive integer")
        if not isinstance(reference, SourceRef):
            raise SourceLifecycleError("Source registration requires a SourceRef")
        await self._register(
            node_id, _Node(kind="source", reference=reference.model_dump(mode="json"), revision=revision)
        )

    async def register_memory(
        self, node_id: str, reference: MemoryCitation, *, parents: tuple[str, ...], policy: str
    ) -> None:
        if not isinstance(reference, MemoryCitation):
            raise SourceLifecycleError("Memory registration requires a MemoryCitation")
        await self._register_derived(node_id, "memory", reference.model_dump(mode="json"), parents, policy)

    async def register_topic(
        self, node_id: str, reference: ArtifactRef, *, parents: tuple[str, ...], policy: str
    ) -> None:
        if not isinstance(reference, ArtifactRef) or reference.family != "topic-memory":
            raise SourceLifecycleError("Topic registration requires a Topic Memory ArtifactRef")
        await self._register_derived(node_id, "topic", reference.model_dump(mode="json"), parents, policy)

    async def _register_derived(
        self,
        node_id: str,
        kind: Literal["memory", "topic"],
        reference: dict[str, JsonValue],
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        _require_text(node_id)
        if not isinstance(parents, (tuple, list)) or not parents:
            raise SourceLifecycleError("derived nodes require distinct registered parents")
        for parent in parents:
            _require_text(parent)
        if len(set(parents)) != len(parents):
            raise SourceLifecycleError("derived nodes require distinct registered parents")
        if node_id in parents or policy not in ("all", "any"):
            raise SourceLifecycleError("invalid dependency policy or self dependency")
        await self._register(
            node_id,
            _Node(
                kind=kind,
                reference=reference,
                parents=tuple(sorted(parents)),
                policy="all" if policy == "all" else "any",
            ),
        )

    async def _register(self, node_id: str, proposed: _Node) -> None:
        from powercontext.builtin.runtime.models import GetMemoryEntryRequest

        _require_text(node_id)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load()
            existing = nodes.get(node_id)
            if existing is not None:
                if existing.model_dump(exclude={"available", "event"}) != proposed.model_dump(
                    exclude={"available", "event"}
                ):
                    raise SourceLifecycleError("node identity is already bound differently")
                await self._synchronize(nodes)
                return
            if any(parent not in nodes for parent in proposed.parents):
                raise SourceLifecycleError("all parents must already be registered")
            try:
                if proposed.kind == "source":
                    reference = SourceRef.model_validate(proposed.reference)
                    await self._runtime.records.for_scope(self.scope_id).get_source(
                        reference.source_type, reference.source_id
                    )
                elif proposed.kind == "memory":
                    citation = MemoryCitation.model_validate(proposed.reference)
                    await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
                else:
                    topic = ArtifactRef.model_validate(proposed.reference)
                    await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                        topic.family, topic.artifact_id, topic.revision
                    )
            except Exception as error:
                raise SourceLifecycleError("registration reference must exist and be readable") from error
            nodes[node_id] = proposed
            _evaluate(nodes)
            await self._save(nodes)
            await self._synchronize(nodes)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply requires a SourceLifecycleEvent")
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load()
            node = nodes.get(event.node_id)
            if node is None or node.kind != "source" or event.revision != node.revision:
                raise SourceLifecycleError("unknown Source or lifecycle revision mismatch")
            if node.event is not None and (
                event.sequence < node.event.sequence or (event.sequence == node.event.sequence and event != node.event)
            ):
                raise SourceLifecycleError("event sequence must increase, or exactly replay its original event")
            if node.event != event:
                node.event = event
                node.available = event.available
                _evaluate(nodes)
                # Persist the intended transition before native writes. A retry
                # replays only missing native state changes after an interruption.
                await self._save(nodes)
            await self._synchronize(nodes)
            return sorted(key for key, item in nodes.items() if item.available)

    async def _synchronize(self, nodes: dict[str, _Node]) -> None:
        memories = tuple(node for node in nodes.values() if node.kind == "memory")
        if not memories:
            return
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            current = await service.head(context.artifacts.memory_artifact_id)
            for available in (False, True):
                manifest = {entry.entry_id: entry for entry in current.content.manifest.entries}
                versions = {entry.entry_id: entry for entry in await service.entries(current)}
                changes = []
                for node in memories:
                    citation = MemoryCitation.model_validate(node.reference)
                    entry = manifest.get(citation.entry_id)
                    version = versions.get(citation.entry_id)
                    if entry is None or version is None or version.entry_version_id != citation.entry_version_id:
                        raise SourceLifecycleError("registered Memory body version changed")
                    if node.available == available and (entry.state == "active") != available:
                        changes.append(version)
                if changes:
                    operation = service.reactivate if available else service.forget
                    current = await operation(current, entries=tuple(changes), reason="Source lifecycle propagation")

    async def resolve(self, node_id: str, *, purpose: str) -> SourceRef | MemoryCitation | ArtifactRef:
        _require_text(node_id)
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown lifecycle resolution purpose")
        async with self._runtime._scope_operation(self.scope_id):
            node = (await self._load()).get(node_id)
            if node is None or (purpose == "generation" and not node.available):
                raise SourceLifecycleError("node is unknown or unavailable for generation")
            reference_type = {"source": SourceRef, "memory": MemoryCitation, "topic": ArtifactRef}[node.kind]
            return reference_type.model_validate(node.reference)


class SourceLifecycleApplication:
    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase | None) -> None:
        self._runtime, self._database = runtime, database

    def for_scope(self, scope_id: str) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, self._database, scope_id)


__all__ = ["SourceLifecycleError", "SourceLifecycleEvent"]
