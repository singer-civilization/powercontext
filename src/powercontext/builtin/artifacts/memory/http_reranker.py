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

"""Identity-preserving Memory reranking through a batched HTTP scoring service."""

from __future__ import annotations

import math
from typing import cast

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    """Score each original candidate once and select across all response batches."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("rerank batch_size must be a positive integer")  # noqa: TRY003
        self.policy_id = policy_id
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Return global ranks ordered by descending score, then original rank."""

        if not candidates or type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("rerank limit must be between one and the non-empty candidate count")  # noqa: TRY003
        scores: dict[int, int | float] = {}
        request_count = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = [
                {"rank": rank, "text": candidate.text}
                for rank, candidate in enumerate(candidates[start : start + self._batch_size], start=start + 1)
            ]
            response = await self._client.post(
                self._endpoint, json={"query": query, "candidates": batch}, follow_redirects=False
            )
            request_count += 1
            response.raise_for_status()
            scores.update(
                _batch_scores(
                    response.json(object_pairs_hook=_unique_object), set(range(start + 1, start + len(batch) + 1))
                )
            )
        selected = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(selected_ranks=tuple(selected), usage=InferenceUsage(requests=request_count))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("rerank response objects must not repeat fields")  # noqa: TRY003
        result[key] = value
    return result


def _batch_scores(value: object, expected: set[int]) -> dict[int, int | float]:
    if not isinstance(value, dict):
        raise ValueError("rerank response must contain only a scores array")  # noqa: TRY003, TRY004
    payload = cast(dict[str, object], value)
    records = payload.get("scores")
    if set(payload) != {"scores"} or not isinstance(records, list):
        raise ValueError("rerank response must contain only a scores array")  # noqa: TRY003
    scores: dict[int, int | float] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"rank", "score"}:
            raise ValueError("rerank score records must contain only rank and score")  # noqa: TRY003
        item = cast(dict[str, object], record)
        rank, score = item["rank"], item["score"]
        if type(rank) is not int or rank not in expected or rank in scores:
            raise ValueError("rerank ranks must uniquely cover the current request batch")  # noqa: TRY003
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("rerank scores must be finite numbers")  # noqa: TRY003, TRY004
        if isinstance(score, float) and not math.isfinite(score):
            raise ValueError("rerank scores must be finite numbers")  # noqa: TRY003
        scores[rank] = score
    if scores.keys() != expected:
        raise ValueError("rerank response must cover every rank in the request batch")  # noqa: TRY003
    return scores


__all__ = ["HTTPMemoryReranker"]
