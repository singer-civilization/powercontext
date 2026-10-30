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

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import PreparedContextBuilder, PreparedMemoryCandidates
from powercontext.http._generated.models import PrepareContextRequest as WirePrepareContextRequest
from powercontext.server.mapping import prepare_context_request


def _group(text: str, scope_id: str = "scope") -> PreparedMemoryCandidates:
    memory_ref = ArtifactRef(family="memory", artifact_id="memory", revision=2)
    return PreparedMemoryCandidates(
        scope_id=scope_id,
        memory_ref=memory_ref,
        hits=(
            MemoryHit(
                memory_ref=memory_ref,
                entry_id="entry",
                entry_version_id="version",
                text=text,
                score=1.0,
                matched_by=("fts",),
            ),
        ),
    )


def test_query_selects_literal_late_sentences_and_preserves_character_offsets() -> None:
    text = (
        "Administrative background. " * 100
        + "海洋 station records cobalt at 37 degrees. "
        + "The amber valve opens at 19 degrees."
    )
    group = _group(text)
    outputs = []
    for query in ("cobalt 37", "amber 19"):
        result = PreparedContextBuilder().build_scopes_result(
            request=PrepareContextRequest(query=query, max_bytes=1000, evidence_selection=True),
            current_scope_id="scope",
            memory_candidates=(group,),
        )
        assert result.fragments and result.context.content_bytes <= 1000
        assert len(result.fragments) == len(result.origins)
        for fragment, origin in zip(result.fragments, result.origins, strict=True):
            assert fragment.text == text[fragment.start : fragment.end]
            assert type(fragment.start) is int and type(fragment.end) is int
            assert fragment.address == origin
        outputs.append(" ".join(fragment.text for fragment in result.fragments))
    assert "cobalt" in outputs[0] and "amber" in outputs[1]


def test_same_text_and_entry_ids_in_different_scopes_keep_separate_citations() -> None:
    text = "The cobalt sensor records 海洋 temperatures at 37 degrees."
    result = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor", max_bytes=1800, evidence_selection=True),
        current_scope_id="left",
        memory_candidates=(_group(text, "left"), _group(text, "right")),
    )
    assert {fragment.address.memory.scope_id for fragment in result.fragments} == {"left", "right"}
    assert result.context.content is not None
    items = json.loads(result.context.content.splitlines()[-2])["items"]
    for item, fragment, origin in zip(items, result.fragments, result.origins, strict=True):
        assert item["citation"]["scope_id"] == fragment.address.memory.scope_id
        assert item["citation"]["memory_ref"] == fragment.address.memory.artifact.model_dump(mode="json")
        assert item["content"] == text[item["start"] : item["end"]]
        assert origin == fragment.address


def test_sentence_selection_matches_common_inflections_without_rewriting() -> None:
    text = "Background observations. " * 80 + "Enzyme activation increased its activity."
    result = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="activated enzyme activities", max_bytes=1000, evidence_selection=True),
        current_scope_id="scope",
        memory_candidates=(_group(text),),
    )
    assert any(fragment.text == "Enzyme activation increased its activity." for fragment in result.fragments)


def test_complete_utf8_budget_includes_envelope_and_can_return_empty() -> None:
    text = "海洋 station records 海洋 station temperatures. " * 30
    for budget in (512, 600, 800, 1200):
        result = PreparedContextBuilder().build_scopes_result(
            request=PrepareContextRequest(query="海洋 station", max_bytes=budget, evidence_selection=True),
            current_scope_id="scope",
            memory_candidates=(_group(text),),
        )
        assert len((result.context.content or "").encode()) == result.context.content_bytes <= budget
        if result.context.content:
            assert "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1" in result.context.content
            assert "END_POWERCONTEXT_PREPARED_CONTEXT_V1" in result.context.content
            assert json.loads(result.context.content.splitlines()[-2])["trust"] == "untrusted_history"
    empty = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="unmatched", evidence_selection=True),
        current_scope_id="scope",
        memory_candidates=(_group(text),),
    )
    assert empty.context.content is None and empty.fragments == empty.origins == ()


def test_default_behavior_and_http_selection_flag_remain_explicit() -> None:
    group = _group("The cobalt sensor operates normally.")
    ordinary = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt"), current_scope_id="scope", memory_candidates=(group,)
    )
    assert ordinary.fragments == ()
    assert ordinary.context.content is not None
    assert json.loads(ordinary.context.content.splitlines()[-2])["items"][0]["content"] == group.hits[0].text
    assert not PrepareContextRequest(query="query").evidence_selection
    wire = WirePrepareContextRequest(scope_id="scope", query="query", evidence_selection=True)
    assert prepare_context_request(wire).evidence_selection
    with pytest.raises(ValidationError):
        PrepareContextRequest(query="query", evidence_selection=1)  # ty: ignore[invalid-argument-type]
