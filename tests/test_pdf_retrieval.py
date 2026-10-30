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
from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    RetireMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    SourceObservation,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFPageInput, search_pdf_pages
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class _PagePipeline:
    def __init__(self) -> None:
        self.duplicate_pages: set[str] = set()

    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            if not isinstance(source, SourceObservation) or source.source_type != "pdf-page":
                continue
            evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            entries.append(MemoryEntryInput(kind="page", text=evidence.content, sources=(source,)))
            if source.name in self.duplicate_pages:
                entries.append(
                    MemoryEntryInput(
                        kind="page-detail", text=evidence.content + "\nAdditional detail.", sources=(source,)
                    )
                )
        return tuple(entries)


def test_retrieval_preserves_page_revision_identity_and_scope_after_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        settings = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pages.db'}"))
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        pipeline = _PagePipeline()
        async with open_builtin_runtime(settings, candidate_pipeline=pipeline) as runtime:
            assert runtime.scopes is not None
            left = await runtime.scopes.create(ScopeDraft(title="Left", summary="PDF pages", idempotency_key="left"))
            right = await runtime.scopes.create(ScopeDraft(title="Right", summary="PDF pages", idempotency_key="right"))
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            expected_sources = set()
            inactive_source = None
            right_versions = set()
            for scope_id, document_id, digest, page_number, active in (
                (left.scope_id, "REPORT_A_2024", "a" * 64, 1, True),
                (left.scope_id, "REPORT_A_2024", "b" * 64, 1, True),
                (left.scope_id, "REPORT_B_2024", "a" * 64, 1, True),
                (left.scope_id, "REPORT_A_2024", "a" * 64, 2, False),
                (right.scope_id, "REPORT_A_2024", "a" * 64, 1, True),
            ):
                page = PDFPageInput(
                    document_id=document_id,
                    document_sha256=digest,
                    page_number=page_number,
                    text="Cobalt sensor financial operating cash flows.",
                )
                observation = project_source_for_transport(registry, await registry.resolve(page))
                if (
                    scope_id == left.scope_id
                    and digest == "a" * 64
                    and page_number == 1
                    and document_id == "REPORT_A_2024"
                ):
                    pipeline.duplicate_pages.add(observation.name)
                await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope_id, observation=observation))
                memory = runtime.memory.for_scope(scope_id)
                while (await memory.flush()).processed:
                    pass
                records = [
                    record
                    for record in (await memory.list()).entries
                    if any(reference.source_id == observation.name for reference in record.entry.sources)
                ]
                assert records
                if scope_id == right.scope_id:
                    right_versions.update(record.entry.entry_version_id for record in records)
                elif active:
                    expected_sources.add(observation.name)
                else:
                    inactive_source = observation.name
                    for record in records:
                        await memory.retire(RetireMemoryEntryRequest(citation=record.citation))
            await runtime.memory.for_scope(left.scope_id).remember(
                RememberMemoryRequest(
                    entries=(
                        MemoryEntryInput(
                            kind="fact", text="Cobalt sensor financial operating cash flows outside any PDF."
                        ),
                    )
                )
            )

        async with open_builtin_runtime(settings) as runtime:
            memory = runtime.memory.for_scope(left.scope_id)
            question = (
                "What does the annual report say about the cobalt sensor, taking the perspective of an investment "
                "analyst who can only rely on available statements and provide a concise financial explanation?"
            )
            citations = await search_pdf_pages(memory, question, limit=20)
            assert isinstance(citations, tuple) and len(citations) == 3
            seen = set()
            for citation in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                assert entry.entry_version_id not in right_versions
                assert len(entry.sources) == 1 and entry.sources[0].source_type == "pdf-page"
                source_id = entry.sources[0].source_id
                assert source_id != inactive_source and source_id not in seen
                seen.add(source_id)
            assert seen == expected_sources
            assert len(await search_pdf_pages(memory, question, limit=2)) == 2

    asyncio.run(scenario())


def test_financial_concepts_recall_complementary_statements_and_natural_synonyms() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Financial pages", summary="Concept retrieval", idempotency_key="financial-concepts")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            memory = runtime.memory.for_scope(scope.scope_id)
            sources = {}
            texts = {
                "income": "CONSOLIDATED STATEMENTS OF INCOME\nNet sales 900. Operating income 140. Net income 85.",
                "cashflow": (
                    "CONSOLIDATED STATEMENTS OF CASH FLOWS\nDepreciation and amortization 35. "
                    "Purchases of property, plant and equipment: capital expenditures 50."
                ),
                "geography": (
                    "Business operations are organized in North America, Europe and Asia Pacific. "
                    "Products are sold in 90 countries through regional segments."
                ),
                "legal": (
                    "Legal proceedings\nLitigation and claims are pending in several jurisdictions. "
                    "Management evaluates contingent liabilities from an ongoing antitrust lawsuit."
                ),
                "workforce": "Human resources\nEmployees receive annual training and benefits. Headcount was 700.",
                "shares": "Common shares and dividends\nThe number of outstanding shares was 250 million.",
                "signatures": "Signatures\nThe directors signed this annual report on behalf of the company.",
            }
            for number, (label, text) in enumerate(texts.items(), 1):
                page = PDFPageInput(
                    document_id="ACME_2024_ANNUAL",
                    document_sha256="c" * 64,
                    page_number=number,
                    text=text,
                )
                observation = project_source_for_transport(registry, await registry.resolve(page))
                sources[label] = observation.name
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            while (await memory.flush()).processed:
                pass

            async def locations(question: str, limit: int) -> set[str]:
                result = set()
                for citation in await search_pdf_pages(memory, question, limit=limit):
                    entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                    result.update(reference.source_id for reference in entry.sources)
                return result

            financial = await locations(
                "Locate the statements needed to derive EBITDA and subtract capex for ACME in 2024.", 2
            )
            assert financial == {sources["income"], sources["cashflow"]}
            geography = await locations(
                "Where in the world does ACME conduct business, and which countries and regions form its principal geographic markets?",
                1,
            )
            assert geography == {sources["geography"]}
            legal = await locations(
                "What pending lawsuits or legal disputes does ACME face during the reporting period?", 1
            )
            assert legal == {sources["legal"]}

    asyncio.run(scenario())


def test_document_header_matches_do_not_recall_blank_or_unrelated_pages() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Blank pages", summary="Header routing", idempotency_key="blank-page-routing")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            expected = None
            for number, text in enumerate(
                (
                    "",
                    "\n   \n",
                    "Operating income was 150 in the current period.",
                    "Employees attended training courses.",
                ),
                1,
            ):
                page = PDFPageInput(
                    document_id="ACME_2024_ANNUAL", document_sha256="d" * 64, page_number=number, text=text
                )
                observation = project_source_for_transport(registry, await registry.resolve(page))
                if number == 3:
                    expected = observation.name
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
            memory = runtime.memory.for_scope(scope.scope_id)
            while (await memory.flush()).processed:
                pass
            citations = await search_pdf_pages(memory, "What is ACME's operating income for 2024?", limit=20)
            assert len(citations) == 1
            entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
            assert len(entry.sources) == 1 and entry.sources[0].source_id == expected

            empty_scope = await runtime.scopes.create(
                ScopeDraft(title="Entirely blank", summary="No body text", idempotency_key="blank-only-pages")
            )
            for number in (1, 2):
                page = PDFPageInput(
                    document_id="EMPTY_CORP_2025", document_sha256="e" * 64, page_number=number, text=""
                )
                observation = project_source_for_transport(registry, await registry.resolve(page))
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=empty_scope.scope_id, observation=observation)
                )
            empty_memory = runtime.memory.for_scope(empty_scope.scope_id)
            while (await empty_memory.flush()).processed:
                pass
            assert len((await empty_memory.list()).entries) == 2
            assert await search_pdf_pages(empty_memory, "What did EMPTY CORP report in 2025?") == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [-1, True, False, 1.5, "2"])
def test_retrieval_limit_requires_a_nonnegative_integer(limit: object) -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError):
            await search_pdf_pages(object(), "annual report", limit=limit)  # ty: ignore[invalid-argument-type]

    asyncio.run(scenario())


def test_zero_limit_needs_no_storage_and_blank_questions_are_rejected() -> None:
    async def scenario() -> None:
        assert await search_pdf_pages(object(), "annual report", limit=0) == ()  # ty: ignore[invalid-argument-type]
        for question in ("", " ", "\t\n"):
            with pytest.raises(ValueError):
                await search_pdf_pages(object(), question)  # ty: ignore[invalid-argument-type]

    asyncio.run(scenario())
