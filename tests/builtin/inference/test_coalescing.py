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

"""Shared provider work, per-caller usage, and cancellation at embedding boundaries."""

from __future__ import annotations

import asyncio
from typing import Literal, cast

import pytest

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class _Provider:
    profile = EmbeddingProfile(profile_id="coalescing-regression", model="offline", dimension=1)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=3, input_tokens=7, output_tokens=0),
        )


def test_late_subscriber_shares_running_work_with_cache_disabled() -> None:
    class Blocked(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.started.set()
            await self.release.wait()
            return await super().embed(texts)

    async def run() -> None:
        base = Blocked()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        first = asyncio.create_task(model.embed(("shared", "another", "shared")))
        await base.started.wait()
        late = asyncio.create_task(model.embed(("another", "shared")))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        base.release.set()
        result = await asyncio.wait_for(late, timeout=2)
        assert result.vectors == ((7.0,), (6.0,))
        assert result.usage == InferenceUsage(requests=3, input_tokens=7, output_tokens=0)
        assert base.calls == [("shared", "another")]
        await model.embed(("shared",))
        assert base.calls == [("shared", "another"), ("shared",)]

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["exception", "count"])
def test_one_failed_batch_does_not_poison_independent_batches_or_retry(failure: Literal["exception", "count"]) -> None:
    class Flaky(_Provider):
        broken = True

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            if "bad" in texts and self.broken:
                self.calls.append(texts)
                if failure == "exception":
                    raise RuntimeError("temporarily unavailable")  # noqa: TRY003
                return EmbeddingResult(vectors=())
            return await super().embed(texts)

    async def run() -> None:
        base = Flaky()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        broken, good = await asyncio.gather(model.embed(("bad",)), model.embed(("good",)), return_exceptions=True)
        assert isinstance(broken, RuntimeError if failure == "exception" else ValueError)
        assert isinstance(good, EmbeddingResult)
        assert good.vectors == ((4.0,),)
        assert (await model.embed(("good",))).usage.requests == 0
        base.broken = False
        assert (await model.embed(("bad",))).vectors == ((3.0,),)
        assert base.calls.count(("bad",)) == 2

    asyncio.run(run())


def test_usage_counts_each_waited_batch_once_and_preserves_unknown_totals() -> None:
    class Metered(_Provider):
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            result = await super().embed(texts)
            if "unknown" in texts:
                result.usage.input_tokens = None
            return result

    async def run() -> None:
        base = Metered()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
        result = await model.embed(("one", "two", "one", "three", "unknown", "three"))
        assert result.usage == InferenceUsage(requests=6, input_tokens=None, output_tokens=0)
        assert result.vectors == ((3.0,), (3.0,), (3.0,), (5.0,), (7.0,), (5.0,))
        cached = await model.embed(("one", "two", "three", "unknown"))
        assert cached.usage.requests == 0
        assert len(base.calls) == 2

    asyncio.run(run())


def test_query_vectors_remain_distinct_for_asymmetric_models() -> None:
    class Asymmetric(_Provider):
        async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(texts)
            return EmbeddingResult(vectors=tuple((-float(len(text)),) for text in texts))

    async def run() -> None:
        base = Asymmetric()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        document, query = await asyncio.gather(model.embed(("same",)), model.embed_query(("same",)))
        assert document.vectors == ((4.0,),)
        assert query.vectors == ((-4.0,),)
        assert (await model.embed_query(("same",))).vectors == ((-4.0,),)
        assert (await model.embed(("same",))).vectors == ((4.0,),)
        assert len(base.calls) == 2

    asyncio.run(run())


def test_cancelled_only_waiter_does_not_leave_an_unhandled_provider_exception() -> None:
    class Failing(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()

        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.started.set()
            await self.release.wait()
            self.finished.set()
            raise RuntimeError("provider failed after caller cancellation")  # noqa: TRY003

    async def run() -> None:
        errors: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        base = Failing()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        waiter = asyncio.create_task(model.embed(("text",)))
        await base.started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        base.release.set()
        await base.finished.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert errors == []

    asyncio.run(run())


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_batch_size_requires_a_positive_integer(value: object) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(_Provider(), max_batch_size=cast(int, value))


@pytest.mark.parametrize("value", [-1, False, 1.5])
def test_cache_capacity_requires_a_non_negative_integer(value: object) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(_Provider(), cache_size=cast(int, value))


@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan"), True, "5"])
def test_coalescing_window_requires_a_finite_non_negative_duration(value: object) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(_Provider(), coalesce_ms=cast(float, value))
