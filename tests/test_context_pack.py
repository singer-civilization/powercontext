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

import asyncio
import json

import pytest

from powercontext.builtin.runtime import BuiltinConfig, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft


@pytest.mark.parametrize("chunk_bytes", [True, False, 0, -1, 1.5])
def test_chunk_limit_requires_a_positive_integer(chunk_bytes: object) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("text", chunk_bytes=chunk_bytes)  # ty: ignore[invalid-argument-type]


def test_utf8_chunks_keep_every_character_and_deterministic_boundaries() -> None:
    text = "a海洋😀e\u0301\nparagraph\u2028tail" * 30
    for limit in range(4, 17):
        pack = ContextPack.from_content(text, chunk_bytes=limit)
        assert b"".join(pack.items).decode() == text
        assert all(0 < len(item) <= limit and item.decode() for item in pack.items)
        assert ContextPack.from_content(text, chunk_bytes=limit) == pack
        assert ContextPack.from_bytes(pack.to_bytes()) == pack
        assert pack.byte_count == len(pack.to_bytes())
    with pytest.raises(ContextPackError):
        ContextPack.from_content("😀", chunk_bytes=3)
    with pytest.raises(ContextPackError):
        ContextPack.build((b"\xff",))


@pytest.mark.parametrize(
    "changes",
    [
        {"byte_count": True},
        {"byte_count": 1.5},
        {"byte_count": 0},
        {"digest": "sha256:" + "f" * 64},
        {"schema": "unknown"},
        {"extra": "unexpected"},
        {"items": ["_w=="]},
        {"items": ["/w=="]},
        {"items": ["YR=="]},
        {"items": [1]},
    ],
)
def test_full_pack_rejects_malformed_fields_and_chunks(changes: dict[str, object]) -> None:
    payload = {**json.loads(ContextPack.build((b"a",)).to_bytes()), **changes}
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps(payload).encode())


def test_wire_decoding_normalizes_json_but_validates_declared_canonical_size() -> None:
    pack = ContextPack.build((b"alpha", "海洋".encode()))
    assert ContextPack.from_bytes(json.dumps(json.loads(pack.to_bytes()), indent=2).encode()) == pack
    for raw in (b"{}", b"[]", b"null", b"\xff", b'{"schema":"a","schema":"b"}'):
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(raw)


def test_reconstruction_validates_prefix_target_and_rebuilt_wire() -> None:
    base = ContextPack.from_content("stable " * 200 + "old tail")
    target = ContextPack.from_content("stable " * 200 + "new tail")
    delta = ContextPackDelta.between(base, target)
    assert delta.prefix_items > 0
    assert (
        apply_context_pack_delta(ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes()))
        == target
    )
    for change in ({"prefix_items": len(base.items) + 1}, {"prefix_items": False}, {"appended": ["/"]}, {"extra": 1}):
        with pytest.raises(ContextPackError):
            decoded = ContextPackDelta.from_bytes(json.dumps({**json.loads(delta.to_bytes()), **change}).encode())
            apply_context_pack_delta(base, decoded)
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, "sha256:" + "0" * 64, 0, (b"wrong",)))


def test_native_incremental_prepare_handles_a_legal_empty_context() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Empty", summary="Empty", idempotency_key="pack-empty")
            )
            context = runtime.context.for_scope(scope.scope_id)
            request = PrepareContextRequest(query="absent")
            first = await context.prepare_incremental(request)
            assert isinstance(first, ContextPack) and first.items == ()
            second = await context.prepare_incremental(request, base_pack=ContextPack.from_content("old"))
            assert isinstance(second, ContextPackDelta)
            rebuilt = apply_context_pack_delta(
                ContextPack.from_content("old"), ContextPackDelta.from_bytes(second.to_bytes())
            )
            assert rebuilt.items == ()

    asyncio.run(scenario())
