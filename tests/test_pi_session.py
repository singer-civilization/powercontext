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
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import JsonValue

from powercontext.errors import ConnectorSubmissionRejectedError
from powercontext.sources import (
    ConnectorBinding,
    SourceDefinitionRegistry,
    SourceMaterialization,
    SourceObservation,
    SourceRef,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle, ConnectorRunStatus
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


def _message(event_id: str, parent_id: str | None, text: str) -> dict[str, JsonValue]:
    return {
        "type": "message",
        "id": event_id,
        "parentId": parent_id,
        "message": {"role": "user", "content": text},
    }


def _archive(records: list[dict[str, JsonValue]], *, session_id: str = "session") -> bytes:
    header = {"type": "session", "version": 3, "id": session_id}
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in [header, *records]).encode("utf-8")


def test_original_text_unicode_separators_and_all_visible_blocks() -> None:
    records: list[dict[str, JsonValue]] = [
        _message("a", None, "  request\u2028with separator\u2029  "),
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "before "},
                    {"type": "toolCall", "name": "read", "id": "call", "arguments": {"z": 1, "a": "中文"}},
                    {"type": "image", "data": "private"},
                    {"type": "text", "text": " after"},
                ],
            },
        },
        {"type": "compaction", "id": "c", "parentId": "b", "summary": "summary", "firstKeptEntryId": "c"},
    ]
    events = parse_pi_session(PiSessionInput(content=b"\xef\xbb\xbf" + _archive(records)))
    assert [event.content for event in events] == [
        "user:   request\u2028with separator\u2029  ",
        'assistant: before \n[tool_call read call] {"a":"中文","z":1}\n after',
        "compaction: summary",
    ]
    assert [event.record for event in events] == records


@pytest.mark.parametrize(
    "suffix",
    [
        b'\n{"type":"model_change","id":"bad"}',
        b'\n{"type":"model_change","id":"bad","parentId":"later"}',
        b'\n{"type":"model_change","id":"bad","parentId":"bad"}',
        b'\n{"type":"model_change","id":"a","parentId":null}',
        b'\n{"type":"model_change","id":"bad","parentId":null,"value":NaN}',
        b'\n{"type":"model_change","id":"bad","parentId":null,"value":Infinity}',
        b'\n{"type":"model_change","id":"bad","parentId":null,"value":-Infinity}',
        b'\n{"type":"model_change","id":"bad","id":"other","parentId":null}',
        b"\n{invalid JSON}",
    ],
)
def test_explicit_leaf_still_validates_unselected_records(suffix: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=_archive([_message("a", None, "visible")]) + suffix, leaf_id="a"))


def test_control_nodes_and_empty_messages_preserve_the_branch() -> None:
    records: list[dict[str, JsonValue]] = [
        _message("a", None, "root"),
        {"type": "model_change", "id": "b", "parentId": "a"},
        {
            "type": "message",
            "id": "c",
            "parentId": "b",
            "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "private"}]},
        },
        _message("d", "c", "tail"),
    ]
    events = parse_pi_session(PiSessionInput(content=_archive(records)))
    assert [event.event_id for event in events] == ["a", "d"]
    assert events[-1].parent_id == "c"
    assert [event.event_id for event in parse_pi_session(PiSessionInput(content=_archive(records), leaf_id="b"))] == [
        "a"
    ]
    assert parse_pi_session(PiSessionInput(content=_archive([]))) == ()


def test_source_retains_large_tool_results_and_isolated_original_record() -> None:
    async def scenario() -> None:
        full_text = "完整工具结果\n" * 5000
        record: dict[str, JsonValue] = {
            "type": "message",
            "id": "tool",
            "parentId": None,
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call",
                "content": [{"type": "text", "text": full_text}],
                "isError": False,
            },
            "provider_extra": {"keep": [1, None, True]},
        }
        event = parse_pi_session(PiSessionInput(content=_archive([record])))[0]
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        source = await registry.resolve(event)
        assert source.materialization is SourceMaterialization.CAPTURED
        observation = project_source_for_transport(registry, source)
        projection = observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
        assert isinstance(projection, dict)
        assert projection["content"] == f"toolResult: [tool_result read call] {full_text}"
        assert len(event.content.encode("utf-8")) > 8192
        assert await registry.read(source) == event
        original = deepcopy(event)
        event.record["provider_extra"] = "mutated by caller"
        reread = await registry.read(source)
        assert reread == original
        assert isinstance(reread, PiEventInput)
        reread.record["provider_extra"] = "mutated reader result"
        assert await registry.read(source) == original

    asyncio.run(scenario())


def test_identity_uses_only_exact_session_and_event_ids() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        identities = []
        for session_id, event_id in [("a:b", "c"), ("a", "b:c"), ("会话" * 300, "事件" * 300)]:
            event = parse_pi_session(
                PiSessionInput(content=_archive([_message(event_id, None, "before")], session_id=session_id))
            )[0]
            source = await registry.resolve(event)
            changed = await registry.resolve(event.model_copy(update={"content": "after"}))
            assert source.name == changed.name and len(source.name) <= 256
            identities.append(source.name)
        assert len(set(identities)) == 3

    asyncio.run(scenario())


class _CheckpointStore:
    def __init__(self) -> None:
        self.value: JsonValue | None = None
        self.fail_save = False

    async def load(self, binding: ConnectorBinding, /) -> JsonValue | None:
        # Deliberately return the original object to catch mutation on a failed run.
        return self.value

    async def save(
        self, binding: ConnectorBinding, checkpoint: JsonValue | None, /, *, expected: JsonValue | None
    ) -> None:
        assert expected == self.value
        if self.fail_save:
            raise RuntimeError("checkpoint persistence unavailable")  # noqa: TRY003
        self.value = checkpoint


class _CapturingSink:
    def __init__(self) -> None:
        self.registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        self.stored: dict[str, SourceObservation] = {}
        self.reject_event_id: str | None = None
        self.fail_event_id: str | None = None

    async def submit(
        self, binding: ConnectorBinding, item_id: str, definition_name: str, value: object, /
    ) -> SourceRef:
        assert isinstance(value, PiEventInput)
        if value.event_id == self.reject_event_id:
            raise ConnectorSubmissionRejectedError("injected rejection")  # noqa: TRY003
        if value.event_id == self.fail_event_id:
            raise RuntimeError("source persistence unavailable")  # noqa: TRY003
        observation = project_source_for_transport(self.registry, await self.registry.resolve(value))
        assert item_id == observation.name
        if item_id in self.stored:
            assert self.stored[item_id] == observation
        self.stored[item_id] = observation
        return SourceRef(source_type=definition_name, source_id=item_id)


def _binding() -> ConnectorBinding:
    return ConnectorBinding(
        scope_id="scope", binding_id="pi", connector_name="pi-session-history", connector_version="1"
    )


@pytest.mark.parametrize("mutation", ["text", "hidden", "removed"])
def test_unselected_imported_branch_changes_fail_before_new_sources(tmp_path: Path, mutation: str) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        original = [
            _message("root", None, "root"),
            _message("left", "root", "left"),
            _message("right", "root", "right"),
        ]
        path.write_bytes(_archive(original))
        checkpoints = _CheckpointStore()
        sink = _CapturingSink()
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)
        first = await lifecycle.run(PiSessionConnector(path, "left"), _binding())
        before = deepcopy(checkpoints.value)
        changed = deepcopy(original)
        if mutation == "text":
            changed[1] = _message("left", "root", "changed")
        elif mutation == "hidden":
            changed[1] = {"type": "model_change", "id": "left", "parentId": "root"}
        else:
            del changed[1]
        path.write_bytes(_archive(changed))
        with pytest.raises(ValueError, match="changed or removed"):
            await lifecycle.run(PiSessionConnector(path, "right"), _binding())
        assert checkpoints.value == before == first.committed_checkpoint
        assert len(sink.stored) == 2
        path.write_bytes(_archive(original))
        resumed = await lifecycle.run(PiSessionConnector(path, "right"), _binding())
        assert len(resumed.items) == 1 and len(sink.stored) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["reject", "submit", "checkpoint"])
def test_failed_run_preserves_checkpoint_and_retries_exact_sources(tmp_path: Path, failure: str) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        records = [_message("a", None, "first")]
        path.write_bytes(_archive(records))
        checkpoints = _CheckpointStore()
        sink = _CapturingSink()
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)
        await lifecycle.run(PiSessionConnector(path), _binding())
        before = deepcopy(checkpoints.value)
        records.extend([_message("b", "a", "second"), _message("c", "b", "third")])
        path.write_bytes(_archive(records))
        if failure == "reject":
            sink.reject_event_id = "c"
            failed = await lifecycle.run(PiSessionConnector(path), _binding())
            assert failed.status is ConnectorRunStatus.INCOMPLETE
            assert failed.committed_checkpoint == before
        else:
            sink.fail_event_id = "c" if failure == "submit" else None
            checkpoints.fail_save = failure == "checkpoint"
            with pytest.raises(RuntimeError, match="unavailable"):
                await lifecycle.run(PiSessionConnector(path), _binding())
        assert checkpoints.value == before
        sink.reject_event_id = sink.fail_event_id = None
        checkpoints.fail_save = False
        # A fresh lifecycle and connector must recover only from persisted state.
        recovered = await ConnectorLifecycle(sink=sink, checkpoints=checkpoints).run(
            PiSessionConnector(path), _binding()
        )
        assert recovered.status is ConnectorRunStatus.COMPLETE
        assert len(sink.stored) == 3
        replayed = await lifecycle.run(PiSessionConnector(path), _binding())
        assert replayed.items == ()

    asyncio.run(scenario())
