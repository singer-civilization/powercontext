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

"""Complete dense PDF evidence survives Memory storage and projection recovery."""

from __future__ import annotations

import asyncio
import io
import unicodedata
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryCitation, MemoryEntryInput
from powercontext.builtin.artifacts.memory.canonical import (
    entry_content_bytes,
    entry_content_hash,
    normalize_entry_text,
    normalize_text,
)
from powercontext.builtin.artifacts.memory.errors import InvalidMemoryCandidateError
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.limits import MAX_SOURCE_OBSERVATION_BYTES
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFDocumentInput, parse_pdf_document


def _dense_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    # A real ToUnicode map emits a decomposed accent from a single PDF glyph.
    # This catches normalization that would alter the source's exact text.
    cmap = DecodedStreamObject()
    cmap.set_data(b"""
/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Evidence-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
1 beginbfchar
<65> <00650301>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end
""")
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/ToUnicode"): writer._add_object(cmap),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
    })
    stream = DecodedStreamObject()
    text = "Revenue 12,345.67 cash 2026. " * 700
    stream.set_data(f"BT /F1 9 Tf 10 700 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class _PagePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            assert isinstance(source, SourceObservation)
            evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            entries.append(MemoryEntryInput(kind="pdf-page", text=evidence.content, sources=(source,)))
        return tuple(entries)


def test_dense_pdf_retains_exact_unicode_through_restart_citation_and_rebuild(tmp_path: Path) -> None:
    async def scenario() -> None:
        page = parse_pdf_document(PDFDocumentInput(document_id="Cafe\u0301-report", content=_dense_pdf()))[0]
        assert len(page.text.encode()) > 8192
        assert not unicodedata.is_normalized("NFC", page.text)
        registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
        source = await registry.resolve(page)
        observation = project_source_for_transport(registry, source)
        expected = f"{page.document_id}\n{page.text}".strip()
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'dense.db'}"))
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Dense pages", summary="Exact PDF evidence", idempotency_key="dense-pdf-evidence")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
            memory = runtime.memory.for_scope(scope.scope_id)
            assert (await memory.flush()).processed
            assert not (await memory.flush()).processed

        # Open a fresh runtime, so no parsed-page or previous-projection cache can
        # supply the citation body in place of the authoritative stored version.
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            search = await memory.search(SearchMemoryRequest(query="cash", mode="fts", limit=1))
            assert len(search.hits) == 1
            hit = search.hits[0]
            citation = MemoryCitation(
                memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
            )
            record = await memory.get(GetMemoryEntryRequest(citation=citation))
            assert record.entry.text == expected
            version = record.entry
            assert version.sources[0].source_type == "pdf-page"
            assert version.entry_content_hash == entry_content_hash(
                kind=version.kind,
                text=expected,
                source_refs=tuple(ref.model_dump(mode="json") for ref in version.sources),
                artifact_refs=tuple(ref.model_dump(mode="json") for ref in version.artifacts),
            )

        async with open_builtin_contexts(config) as contexts:
            context = await contexts.get(scope.scope_id)
            await context.artifacts.memory.rebuild_projections()
            assert await context.artifacts.memory.validate_citation(citation) == version
            stored = await context.sources.journal.entries()
            assert len(stored) == 1
            persisted = stored[0].source
            assert isinstance(persisted, SourceObservation)
            assert persisted.projection(TEXT_EVIDENCE_PROJECTION_KEY) == observation.projection(
                TEXT_EVIDENCE_PROJECTION_KEY
            )

        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry == version
            search = await memory.search(SearchMemoryRequest(query="cash", mode="fts", limit=1))
            assert [hit.entry_version_id for hit in search.hits] == [version.entry_version_id]

    asyncio.run(scenario())


def test_ordinary_memory_keeps_its_original_byte_limit() -> None:
    async def scenario() -> None:
        async with open_builtin_contexts(BuiltinConfig(database=SQLiteConfig())) as contexts:
            memory = (await contexts.get("ordinary")).artifacts.memory
            with pytest.raises(InvalidMemoryCandidateError, match="8192 UTF-8 bytes"):
                await memory.remember(
                    memory=None, entries=(MemoryEntryInput(kind="fact", text="x" * 8193),), mode="append"
                )

    asyncio.run(scenario())


def test_only_one_unique_pdf_page_gets_the_extended_bound_and_preserves_unicode() -> None:
    ref = {"source_type": "pdf-page", "source_id": "pdf:one"}
    text = " Cafe\u0301 " + "界" * 3000
    assert normalize_entry_text(text, (ref, ref)) == text.strip()
    assert normalize_text("Cafe\u0301") == "Caf\u00e9"
    assert entry_content_bytes(kind="page", text=text, source_refs=(ref, ref), artifact_refs=()) == entry_content_bytes(
        kind="page", text=text, source_refs=(ref,), artifact_refs=()
    )
    for refs in ((), ({"source_type": "csv-row", "source_id": "row"},), (ref, {**ref, "source_id": "pdf:two"})):
        with pytest.raises(ValueError, match="8192 UTF-8 bytes"):
            normalize_entry_text(text, refs)
    assert normalize_entry_text("x" * MAX_SOURCE_OBSERVATION_BYTES, (ref,)) == "x" * MAX_SOURCE_OBSERVATION_BYTES
    with pytest.raises(ValueError, match=f"{MAX_SOURCE_OBSERVATION_BYTES} UTF-8 bytes"):
        normalize_entry_text("界" * (MAX_SOURCE_OBSERVATION_BYTES // 3 + 1), (ref,))
