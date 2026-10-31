#!/usr/bin/env python3
# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0.

"""Run a PDF candidate without exposing evaluation labels to its process.

The manifest contains only ``documents`` (``document_id``, ``path``) and
``questions`` (``question_id``, ``question``). Paths are relative to the manifest.
Run each evaluation group with its own work directory. Existing databases require
``--reuse-persisted`` and must exactly match freshly parsed original PDF bytes.
A separate process may score the JSON against independently held annotations.

Example::

    competition/.venv/bin/python scripts/evaluate_pdf_candidate.py \
        --candidate /path/to/product --manifest /tmp/questions.json \
        --work-dir /tmp/pdf-run --output /tmp/pdf-run.json

This runner calculates structural checks, not quality or recall scores. It uses
the published one-source-per-entry evidence pipeline and the real SQLite Runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
    from powercontext.builtin.runtime import BuiltinConfig
    from powercontext.sources import Source
    from powercontext.sources.observations import SourceObservation


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(manifest, dict), "Manifest must be an object")
    require(set(manifest) == {"documents", "questions"}, "Manifest accepts only documents and questions")
    require(isinstance(manifest["documents"], list) and bool(manifest["documents"]), "No documents supplied")
    require(isinstance(manifest["questions"], list), "Questions must be a list")
    for document in manifest["documents"]:
        require(isinstance(document, dict), "Document must be an object")
        require(set(document) == {"document_id", "path"}, "Document accepts only document_id and path")
        require(all(isinstance(value, str) and value.strip() for value in document.values()), "Empty document value")
    for question in manifest["questions"]:
        require(isinstance(question, dict), "Question must be an object")
        require(set(question) == {"question_id", "question"}, "Question accepts only question_id and question")
        require(all(isinstance(value, str) and value.strip() for value in question.values()), "Empty question value")
    require(
        len({document["document_id"] for document in manifest["documents"]}) == len(manifest["documents"]),
        "Duplicate document_id",
    )
    require(
        len({question["question_id"] for question in manifest["questions"]}) == len(manifest["questions"]),
        "Duplicate question_id",
    )
    return manifest


def text_evidence(source: Source) -> dict[str, Any]:
    from powercontext.sources.observations import SourceObservation
    from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

    require(isinstance(source, SourceObservation), "Fixed pipeline requires a transported Source observation")
    observation = cast(SourceObservation, source)
    return TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)).model_dump()


class EvidencePipeline:
    """The published deterministic pipeline, with no model or scorer access."""

    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        from powercontext.builtin.artifacts.memory import MemoryEntryInput

        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=text_evidence(source)["content"],
                sources=(source,),
            )
            for source in request.sources
            if text_evidence(source)["content"].strip()
        )


async def parse_documents(manifest: dict[str, Any], base_directory: Path):
    from powercontext.sources import SourceDefinitionRegistry, project_source_for_transport
    from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFDocumentInput, parse_pdf_document

    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    observations = []
    documents = []
    expected = {}
    for document in manifest["documents"]:
        path = (base_directory / document["path"]).resolve()
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        pages = parse_pdf_document(PDFDocumentInput(document_id=document["document_id"], content=content))
        require(isinstance(pages, tuple), "PDF parser must return a tuple")
        require(
            [page.page_number for page in pages] == list(range(1, len(pages) + 1)),
            "PDF parser omitted or reordered a physical page",
        )
        report_pages = []
        for page in pages:
            require(page.document_id == document["document_id"], "Parser changed document identity")
            require(page.document_sha256 == digest, "Parser changed original-byte digest")
            source = await registry.resolve(page)
            require(await registry.read(source) == page, "Source readback changed a page")
            require((await registry.resolve(page)).name == source.name, "Source identity is unstable")
            observation = project_source_for_transport(registry, source)
            projection = text_evidence(observation)
            require(projection["source_type"] == "pdf-page", "Unexpected projected Source type")
            require(projection["source_id"] == source.name, "Projection changed Source identity")
            require(projection["content"] == f"{page.document_id}\n{page.text}", "Projection changed page text")
            require(
                projection["metadata"]
                == {
                    "document_id": page.document_id,
                    "document_sha256": digest,
                    "page_number": page.page_number,
                },
                "Projection changed page metadata",
            )
            require(source.name not in expected, "Source identity collision")
            observations.append(observation)
            expected[source.name] = observation
            report_pages.append({"page_number": page.page_number, "source_id": source.name, "text": page.text})
        documents.append({
            "document_id": document["document_id"],
            "document_sha256": digest,
            "page_count": len(pages),
            "text_characters": sum(len(page.text) for page in pages),
            "pages": report_pages,
        })
        del pages, content
    require(bool(observations), "Documents produced no physical pages")
    return documents, observations, expected


async def verify_reusable_sources(
    config: BuiltinConfig, scope_id: str, expected: dict[str, SourceObservation], *, reuse_persisted: bool
) -> None:
    if not reuse_persisted:
        return
    from powercontext.builtin.runtime import open_builtin_contexts

    async with open_builtin_contexts(config) as contexts:
        journal = await (await contexts.get(scope_id)).sources.journal.entries()
        stored = {entry.source_ref.source_id: entry.source for entry in journal}
        require(
            len(journal) == len(expected) and stored == expected,
            "Existing database does not match freshly parsed PDF sources; use a new work directory",
        )


async def evaluate(manifest_path: Path, work_dir: Path, *, reuse_persisted: bool = False) -> dict[str, Any]:
    from powercontext.builtin.persistence.sqlite import SQLiteConfig
    from powercontext.builtin.runtime import (
        BuiltinConfig,
        GetMemoryEntryRequest,
        SubmitSourceObservation,
        open_builtin_contexts,
        open_builtin_runtime,
    )
    from powercontext.builtin.scope import ScopeDraft
    from powercontext.sources import manifest_for_definition
    from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, search_pdf_pages

    manifest = read_manifest(manifest_path)
    database = work_dir / "sources.db"
    started = time.perf_counter()
    documents, observations, expected = await parse_documents(manifest, manifest_path.parent)
    parsed = time.perf_counter()
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    async with open_builtin_runtime(config) as runtime:
        scope_service = runtime.scopes
        if scope_service is None:
            raise RuntimeError("Runtime has no Scope service")  # noqa: TRY003
        scope = await scope_service.create(
            ScopeDraft(
                title="PDF evaluation", summary="Complete original page evidence", idempotency_key="pdf-evaluation"
            )
        )
        await verify_reusable_sources(config, scope.scope_id, expected, reuse_persisted=reuse_persisted)
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for observation in observations:
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
        # Retry every page, so idempotency is verified for the entire collection.
        for observation in observations:
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
    async with open_builtin_runtime(config, candidate_pipeline=EvidencePipeline()) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        for _ in range(len(observations) + 1):
            if not (await memory.flush()).processed:
                break
        else:
            require(False, "Evidence pipeline did not drain the submitted pages")
        require(not (await memory.flush()).processed, "Evidence pipeline left pending observations")
    persisted = time.perf_counter()
    async with open_builtin_contexts(config) as contexts:
        journal = await (await contexts.get(scope.scope_id)).sources.journal.entries()
        require(len(journal) == len(observations), "Retry changed the number of source observations")
        stored = {entry.source_ref.source_id: entry.source for entry in journal}
        require(stored == expected, "Persisted sources changed during Runtime restart")
    queries = []
    all_citations = []
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        records = (await memory.list()).entries
        require(len(records) == len(observations), "Full-page Memory count differs from physical page count")
        memory_source_ids = []
        for record in records:
            require(len(record.entry.sources) == 1, "A full-page Memory entry must cite one Source")
            reference = record.entry.sources[0]
            source = stored[reference.source_id]
            projection = text_evidence(source)
            require(record.entry.text == projection["content"].strip(), "Reopened Memory changed full-page text")
            memory_source_ids.append(reference.source_id)
            reread = await memory.get(GetMemoryEntryRequest(citation=record.citation))
            require(reread.entry == record.entry, "Exact citation changed reopened Memory content")
        require(set(memory_source_ids) == set(stored), "Persisted Memory omitted or duplicated pages")
        for question in manifest["questions"]:
            query_started = time.perf_counter()
            citations = await search_pdf_pages(memory, question["question"], limit=20)
            require(isinstance(citations, tuple) and len(citations) <= 20, "Search violated its limit or return type")
            returned_pages = []
            locations = set()
            for citation in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                require(len(entry.sources) == 1, "A search citation must reference one exact page")
                reference = entry.sources[0]
                source = stored[reference.source_id]
                projection = text_evidence(source)
                require(entry.text == projection["content"].strip(), "Search citation changed original page text")
                metadata = projection["metadata"]
                location = (metadata["document_id"], metadata["document_sha256"], metadata["page_number"])
                require(location not in locations, "Search returned duplicate physical page citations")
                locations.add(location)
                returned_pages.append({
                    **metadata,
                    "source_id": reference.source_id,
                    "citation": citation.model_dump(mode="json"),
                })
                all_citations.append((citation, entry))
            queries.append({
                "question_id": question["question_id"],
                "question": question["question"],
                "limit": 20,
                "returned_pages": returned_pages,
                "search_seconds": time.perf_counter() - query_started,
            })
    # Exact citations must stay usable after the searching Runtime has closed.
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        for citation, original in all_citations:
            require(
                (await memory.get(GetMemoryEntryRequest(citation=citation))).entry == original,
                "Historical search citation changed after another Runtime restart",
            )
    finished = time.perf_counter()
    return {
        "schema_version": 1,
        "status": "passed",
        "reused_persisted_database": reuse_persisted,
        "documents": documents,
        "questions": queries,
        "structural": {
            "documents": len(documents),
            "physical_pages": len(observations),
            "source_journal_entries": len(stored),
            "memory_entries": len(records),
            "duplicate_retries": len(observations),
            "reopened_full_page_readbacks": len(records),
            "reopened_search_citation_readbacks": len(all_citations),
            "questions": len(queries),
        },
        "timing_seconds": {
            "parse_and_source": parsed - started,
            "persistence_and_pipeline": persisted - parsed,
            "reopen_and_search": finished - persisted,
            "total": finished - started,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True, help="Product checkout whose src/ is evaluated")
    parser.add_argument(
        "--manifest", type=Path, required=True, help="Questions and PDF paths only; no evaluation labels"
    )
    parser.add_argument("--work-dir", type=Path, required=True, help="Directory for this group's SQLite database")
    parser.add_argument("--output", type=Path, required=True, help="UTF-8 JSON artifact for an independent scorer")
    parser.add_argument(
        "--reuse-persisted",
        action="store_true",
        help="Reuse a database only after reparsing every PDF and verifying exact persisted Source equality",
    )
    args = parser.parse_args()
    candidate_src = args.candidate.resolve() / "src"
    require(candidate_src.is_dir(), "Candidate checkout has no src directory")
    sys.path.insert(0, str(candidate_src))
    work_dir = args.work_dir.resolve()
    database_exists = (work_dir / "sources.db").exists()
    require(
        not database_exists or args.reuse_persisted,
        "Use a new work directory or --reuse-persisted; sources.db already exists",
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(
        evaluate(args.manifest.resolve(), work_dir, reuse_persisted=database_exists and args.reuse_persisted)
    )
    result["candidate_src"] = str(candidate_src)
    result["python_version"] = sys.version
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({
            "output": str(args.output.resolve()),
            **result["structural"],
            "timing_seconds": result["timing_seconds"],
        })
    )


if __name__ == "__main__":
    main()
