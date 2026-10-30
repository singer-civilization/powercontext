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

"""Capture the original visible events of a Pi v3 session branch."""

# Invalid archive values are reported uniformly as ValueError by this interface.
# ruff: noqa: TRY003, TRY004

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

_SOURCE_NAME = "pi-event"


class PiSessionInput(BaseModel):
    """One complete UTF-8 JSONL archive and the branch to capture."""

    model_config = ConfigDict(frozen=True, strict=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """An original visible event, including its complete provider record."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _require_string(value: object, field: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"{field} must be {'a non-empty string' if nonempty else 'a string'}")
    return value


def _decode_records(content: bytes) -> list[dict[str, JsonValue]]:
    # Split only at JSONL line endings: str.splitlines() would also split literal
    # Unicode paragraph separators inside otherwise valid JSON strings.
    lines = content.decode("utf-8-sig").split("\n")
    records: list[dict[str, JsonValue]] = []
    for line in lines:
        if not line.strip():
            continue
        record = json.loads(line, parse_constant=_reject_json_constant, object_pairs_hook=_json_object)
        if not isinstance(record, dict):
            raise ValueError("Pi session records must be JSON objects")
        records.append(record)
    if not records:
        raise ValueError("Pi session header is missing")
    return records


def _parse_archive(value: PiSessionInput) -> tuple[str, dict[str, dict[str, JsonValue]]]:
    records = _decode_records(value.content)
    header = records[0]
    if header.get("type") != "session" or type(header.get("version")) is not int or header["version"] != 3:
        raise ValueError("Pi sessions require a version 3 session header")
    session_id = _require_string(header.get("id"), "session id", nonempty=True)
    nodes: dict[str, dict[str, JsonValue]] = {}
    for record in records[1:]:
        event_id = _require_string(record.get("id"), "event id", nonempty=True)
        _require_string(record.get("type"), "event type", nonempty=True)
        if event_id in nodes:
            raise ValueError(f"duplicate Pi event id: {event_id!r}")
        if "parentId" not in record:
            raise ValueError(f"Pi event {event_id!r} has no parentId")
        parent_id = record["parentId"]
        if parent_id is not None:
            parent_id = _require_string(parent_id, "parentId", nonempty=True)
            if parent_id not in nodes:
                raise ValueError(f"Pi event {event_id!r} refers to a missing or later parent")
        nodes[event_id] = record
    if value.leaf_id is not None and value.leaf_id not in nodes:
        raise ValueError(f"Pi leaf does not exist: {value.leaf_id!r}")
    return session_id, nodes


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _visible_content(value: JsonValue, *, tool_calls: bool = False) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if not isinstance(value, list):
        raise ValueError("Pi message content must be a string or a list of blocks")
    parts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            raise ValueError("Pi message content blocks must be objects")
        if block.get("type") == "text":
            parts.append(_require_string(block.get("text"), "text block"))
        elif tool_calls and block.get("type") == "toolCall":
            name = _require_string(block.get("name"), "tool name", nonempty=True)
            call_id = _require_string(block.get("id"), "tool call id", nonempty=True)
            if "arguments" not in block:
                raise ValueError("Pi tool calls require arguments")
            parts.append(f"[tool_call {name} {call_id}] {_compact_json(block['arguments'])}")
    return "\n".join(parts)


def _event_text(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    event_type = _require_string(record["type"], "event type")
    if event_type in {"compaction", "branch_summary"}:
        role = str(event_type)
        body = _require_string(record.get("summary", ""), "summary")
    elif event_type == "custom_message":
        role = "custom"
        body = _visible_content(record.get("content"))
    elif event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("Pi message entries require a message object")
        role = _require_string(message.get("role"), "message role", nonempty=True)
        if role == "bashExecution":
            command = _require_string(message.get("command", ""), "bash command")
            output = _require_string(message.get("output", ""), "bash output")
            if not command.strip() and not output.strip():
                return None
            body = f"$ {command}\n{output}"
        elif role == "toolResult":
            text = _visible_content(message.get("content"))
            if not text.strip():
                return None
            name = _require_string(message.get("toolName"), "tool name", nonempty=True)
            call_id = _require_string(message.get("toolCallId"), "tool call id", nonempty=True)
            body = f"[tool_result {name} {call_id}] {text}"
        else:
            body = _visible_content(message.get("content"), tool_calls=True)
    else:
        return None
    return (role, body) if body.strip() else None


def _branch_events(
    session_id: str, nodes: dict[str, dict[str, JsonValue]], leaf_id: str | None
) -> tuple[PiEventInput, ...]:
    current = leaf_id if leaf_id is not None else next(reversed(nodes), None)
    branch: list[dict[str, JsonValue]] = []
    while current is not None:
        record = nodes[current]
        branch.append(record)
        parent_id = record["parentId"]
        current = parent_id if isinstance(parent_id, str) else None
    events: list[PiEventInput] = []
    for record in reversed(branch):
        text = _event_text(record)
        if text is None:
            continue
        role, body = text
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=_require_string(record["id"], "event id"),
                parent_id=None if record["parentId"] is None else _require_string(record["parentId"], "parentId"),
                event_type=_require_string(record["type"], "event type"),
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Validate the whole archive, then return its selected original history."""

    session_id, nodes = _parse_archive(value)
    return _branch_events(session_id, nodes, value.leaf_id)


def _event_identity(event: PiEventInput) -> str:
    identity = _compact_json([event.session_id, event.event_id]).encode("utf-8")
    return f"pi-event:{hashlib.sha256(identity).hexdigest()}"


class PiEventSource(Source):
    """A captured original event, independent of its source file's lifetime."""

    event: PiEventInput


class _PiEventAdapter:
    input_class = PiEventInput
    source_class = PiEventSource
    name = _SOURCE_NAME

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=_event_identity(value),
            materialization=SourceMaterialization.CAPTURED,
            event=value.model_copy(deep=True),
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return source.event.model_copy(deep=True)


class _PiTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        event = source.event
        return TextEvidence(
            source_type=_SOURCE_NAME,
            source_id=source.name,
            content=event.content,
            metadata={
                "session_id": event.session_id,
                "event_id": event.event_id,
                "parent_id": event.parent_id,
                "event_type": event.event_type,
                "role": event.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(_PiEventAdapter(), projections=(_PiTextProjection(),))


class _PiCheckpoint(BaseModel):
    """Record fingerprints retain deduplication across every selected branch."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    sessions: dict[str, dict[str, str]]


def _record_fingerprint(record: dict[str, JsonValue]) -> str:
    return hashlib.sha256(_compact_json(record).encode("utf-8")).hexdigest()


class PiSessionConnector:
    """Submit unseen branch events, leaving checkpoint commits to the lifecycle."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self.path.read_bytes)
        value = PiSessionInput(content=content, leaf_id=self.leaf_id)
        session_id, nodes = _parse_archive(value)
        events = _branch_events(session_id, nodes, value.leaf_id)
        checkpoint = (
            _PiCheckpoint(sessions={})
            if session.checkpoint is None
            else _PiCheckpoint.model_validate(session.checkpoint)
        )
        sessions = {key: dict(imported) for key, imported in checkpoint.sessions.items()}
        imported = sessions.setdefault(session_id, {})
        # Check previously imported records on *all* branches before any write.
        # The original checkpoint is never mutated, even if submission/save fails.
        for event_id, fingerprint in imported.items():
            if event_id not in nodes or _record_fingerprint(nodes[event_id]) != fingerprint:
                raise ValueError(f"previously imported Pi event was changed or removed: {event_id!r}")
        pending = tuple(
            (event, _record_fingerprint(event.record)) for event in events if event.event_id not in imported
        )
        for event, fingerprint in pending:
            outcome = await session.submit(_event_identity(event), _SOURCE_NAME, event)
            if outcome.status is not ConnectorSubmissionStatus.ACCEPTED:
                return ConnectorRunCompletion(status=ConnectorRunStatus.INCOMPLETE, checkpoint=session.checkpoint)
            imported[event.event_id] = fingerprint
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=_PiCheckpoint(sessions=sessions).model_dump(mode="json"),
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
