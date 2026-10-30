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

"""Share pending embedding work and cache successful vectors within one event loop."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel, QueryEmbeddingModel, embed_query

_Kind = Literal["document", "query"]
_Key = tuple[_Kind, str]


@dataclass(frozen=True, slots=True)
class _Embedded:
    vector: EmbeddingVector
    batch_id: int
    usage: InferenceUsage


def _consume_exception(future: asyncio.Future[_Embedded]) -> None:
    # A provider can fail after every caller was cancelled. Retrieving its error
    # prevents an orphan-Future warning without changing what other callers see.
    if not future.cancelled():
        future.exception()


class CoalescingEmbeddingModel:
    """Bound provider batches while isolating callers' cancellation and usage.

    Instances coordinate concurrent calls on the same event loop. Document and
    query embeddings share work only when the underlying model is symmetric.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size <= 0:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        if (
            isinstance(coalesce_ms, bool)
            or not isinstance(coalesce_ms, (int, float))
            or not math.isfinite(coalesce_ms)
            or coalesce_ms < 0
        ):
            raise ValueError("coalesce_ms must be a finite non-negative number")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._delay = coalesce_ms / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[_Key, EmbeddingVector] = OrderedDict()
        self._inflight: dict[_Key, asyncio.Future[_Embedded]] = {}
        self._pending: dict[_Key, asyncio.Future[_Embedded]] = {}
        self._dispatcher: asyncio.Task[None] | None = None
        self._workers: set[asyncio.Task[None]] = set()
        self._batch_id = 0

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed documents, preserving each caller's original order and duplicates."""

        return await self._embed(texts, kind="document")

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Preserve asymmetric providers' query semantics and cache isolation."""

        kind: _Kind = "query" if isinstance(self._model, QueryEmbeddingModel) else "document"
        return await self._embed(texts, kind=kind)

    async def _embed(self, texts: tuple[str, ...], *, kind: _Kind) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=())
        loop = asyncio.get_running_loop()
        positions: list[EmbeddingVector | asyncio.Future[_Embedded]] = []
        waiting: dict[asyncio.Future[_Embedded], None] = {}
        for text in texts:
            key = (kind, text)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                positions.append(cached)
                continue
            future = self._inflight.get(key)
            if future is None:
                future = loop.create_future()
                future.add_done_callback(_consume_exception)
                self._inflight[key] = future
                self._pending[key] = future
            positions.append(future)
            waiting[future] = None
        if self._pending and self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch())

        # Shield every shared Future: cancelling this gather must never cancel
        # the provider work or any other caller's access to its result.
        futures = tuple(waiting)
        results = await asyncio.gather(*(asyncio.shield(future) for future in futures))
        resolved = dict(zip(futures, results, strict=True))
        batches = {result.batch_id: result.usage for result in results}
        vectors = tuple(resolved[item].vector if isinstance(item, asyncio.Future) else item for item in positions)
        return EmbeddingResult(vectors=vectors, usage=_usage(tuple(batches.values())))

    async def _dispatch(self) -> None:
        try:
            # Yield even at zero, allowing calls already scheduled on this loop
            # to register all their inputs before the first batch is dispatched.
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            pending, self._pending = self._pending, {}
            for key, future in pending.items():
                self._inflight.pop(key, None)
                future.cancel()
            raise
        finally:
            self._dispatcher = None
        pending, self._pending = self._pending, {}
        for kind in ("document", "query"):
            selected = tuple((key, future) for key, future in pending.items() if key[0] == kind)
            for start in range(0, len(selected), self._max_batch_size):
                batch = selected[start : start + self._max_batch_size]
                self._batch_id += 1
                task = asyncio.create_task(self._run_batch(batch, self._batch_id))
                self._workers.add(task)
                task.add_done_callback(self._workers.discard)

    async def _run_batch(self, batch: tuple[tuple[_Key, asyncio.Future[_Embedded]], ...], batch_id: int) -> None:
        texts = tuple(key[1] for key, _ in batch)
        try:
            result = await (embed_query(self._model, texts) if batch[0][0][0] == "query" else self._model.embed(texts))
            self._publish(batch, batch_id, result)
        except asyncio.CancelledError:
            for _, future in batch:
                future.cancel()
            raise
        except Exception as error:
            for _, future in batch:
                if not future.done():
                    future.set_exception(error)
        finally:
            for key, future in batch:
                if self._inflight.get(key) is future:
                    del self._inflight[key]

    def _publish(
        self, batch: tuple[tuple[_Key, asyncio.Future[_Embedded]], ...], batch_id: int, result: EmbeddingResult
    ) -> None:
        # Validate the entire response before caching or completing any item.
        if len(result.vectors) != len(batch):
            raise ValueError("embedding provider returned the wrong number of vectors")  # noqa: TRY003
        for (key, future), vector in zip(batch, result.vectors, strict=True):
            if self._cache_size:
                self._cache[key] = vector
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            future.set_result(_Embedded(vector=vector, batch_id=batch_id, usage=result.usage.model_copy()))


def _usage(batches: tuple[InferenceUsage, ...]) -> InferenceUsage:
    if not batches:
        return InferenceUsage(requests=0)
    input_tokens = tuple(batch.input_tokens for batch in batches)
    output_tokens = tuple(batch.output_tokens for batch in batches)
    return InferenceUsage(
        requests=sum(batch.requests for batch in batches),
        input_tokens=None if None in input_tokens else sum(value for value in input_tokens if value is not None),
        output_tokens=None if None in output_tokens else sum(value for value in output_tokens if value is not None),
    )


__all__ = ["CoalescingEmbeddingModel"]
