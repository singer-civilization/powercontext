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

"""HTTP reranking keeps batch-local validation and a global stable selection."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker


def _candidates(count: int) -> tuple[MemoryHit, ...]:
    return tuple(
        MemoryHit(
            memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=2),
            entry_id=f"entry-{rank}",
            entry_version_id=f"version-{rank}",
            text=f"原文 {rank}",
            score=1.0 / rank,
            matched_by=("vector",),
        )
        for rank in range(1, count + 1)
    )


def test_global_selection_includes_last_batch_and_breaks_ties_by_original_rank() -> None:
    seen: list[int] = []
    candidates = _candidates(8)
    expected_scores = {1: 0, 2: -1, 3: 0.8, 4: 0.2, 5: 0.8, 6: 0.3, 7: 0.9, 8: 0.8}

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ranks = [candidate["rank"] for candidate in body["candidates"]]
        assert len(ranks) <= 3
        for candidate in body["candidates"]:
            assert candidate["text"] == candidates[candidate["rank"] - 1].text
        seen.extend(ranks)
        return httpx.Response(
            200,
            json={"scores": [{"rank": rank, "score": expected_scores[rank]} for rank in reversed(ranks)]},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            reranker = HTTPMemoryReranker("https://example.invalid", client=client, batch_size=3, policy_id="test")
            decision = await reranker.rerank("query", candidates, 4)
            assert reranker.policy_id == "test"
            assert decision.selected_ranks == (7, 3, 5, 8)
            assert decision.usage.requests == 3
            assert seen == list(range(1, 9))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"scores": None},
        {"scores": {}},
        {"scores": []},
        {"scores": [{"rank": 1, "score": 1}], "extra": 0},
        {"scores": [None]},
        {"scores": [{"rank": True, "score": 1}]},
        {"scores": [{"rank": 1.0, "score": 1}]},
        {"scores": [{"rank": "1", "score": 1}]},
        {"scores": [{"rank": 1, "score": True}]},
        {"scores": [{"rank": 1, "score": "0.5"}]},
        {"scores": [{"rank": 1, "score": None}]},
        {"scores": [{"rank": 1, "score": float("-inf")}]},
        {"scores": [{"rank": 1, "score": 1, "text": "changed"}]},
    ],
)
def test_response_validation_rejects_schema_and_coercible_wrong_types(payload: object) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(payload).encode()))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://example.invalid", client=client).rerank("query", _candidates(1), 1)

    asyncio.run(scenario())


def test_a_score_from_another_batch_is_rejected() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"scores": [{"rank": 2, "score": 1}]}))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://example.invalid", client=client, batch_size=1).rerank(
                    "query", _candidates(2), 1
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 400, 429, 500])
def test_non_success_responses_keep_httpx_status_errors(status: int) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(status))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await HTTPMemoryReranker("https://example.invalid", client=client).rerank("query", _candidates(1), 1)
            assert error.value.response.status_code == status

    asyncio.run(scenario())


def test_malformed_json_fails_with_value_error() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b'{"scores":'))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://example.invalid", client=client).rerank("query", _candidates(1), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "content",
    [
        b'{"scores":[],"scores":[{"rank":1,"score":1}]}',
        b'{"scores":[{"rank":1,"rank":1,"score":1}]}',
    ],
)
def test_repeated_json_object_fields_are_invalid(content: bytes) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=content))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://example.invalid", client=client).rerank("query", _candidates(1), 1)

    asyncio.run(scenario())


def test_redirects_remain_http_errors_when_the_callers_client_follows_redirects() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(307, headers={"Location": "/other"}))
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await HTTPMemoryReranker("https://example.invalid", client=client).rerank("query", _candidates(1), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, "2"])
def test_invalid_batch_size_fails_before_any_request(batch_size) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://example.invalid", client=client, batch_size=batch_size)

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, -1, 3, True, 1.5])
def test_invalid_limit_fails_before_any_request(limit) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://example.invalid", client=client).rerank("q", _candidates(2), limit)

    asyncio.run(scenario())
