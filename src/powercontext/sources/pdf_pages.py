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

"""Complete physical PDF pages as immutable, revision-specific source evidence."""

from __future__ import annotations

import hashlib
import io
import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pypdf import PageObject, PdfReader
from pypdf.generic import ArrayObject, NullObject, StreamObject

from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.pdf_retrieval import search_pdf_pages
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

_DocumentSHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_PhysicalPageNumber = Annotated[int, Field(ge=1, strict=True)]


class PDFDocumentInput(BaseModel):
    """A complete original PDF and its caller-owned document identity."""

    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _document_id(value)


class PDFPageInput(BaseModel):
    """Text extracted from exactly one physical page of one PDF revision."""

    document_id: str
    document_sha256: _DocumentSHA256
    page_number: _PhysicalPageNumber
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _document_id(value)


class PDFPageSource(Source):
    """Captured page text retained independently of later document revisions."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    document_id: str
    document_sha256: _DocumentSHA256
    page_number: _PhysicalPageNumber
    text: str

    @model_validator(mode="after")
    def validate_identity(self) -> PDFPageSource:
        if self.name != _page_identity(self.document_id, self.document_sha256, self.page_number):
            raise ValueError("PDF page name must match its document, revision, and physical page")  # noqa: TRY003
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract all pages, including blank pages, or fail without partial output."""

    _document_id(value.document_id)
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF document requires a non-empty password")  # noqa: TRY003, TRY301
        return tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=number,
                text=_page_text(page),
            )
            for number, page in enumerate(reader.pages, start=1)
        )
    except Exception as error:
        # Malformed PDFs can produce arbitrary parser exceptions as well as
        # PdfReadError. Normalize failures at this complete-document boundary.
        raise ValueError("PDF document cannot be completely parsed") from error  # noqa: TRY003


def _page_text(page: PageObject) -> str:
    # pypdf treats a malformed /Contents scalar as an empty page even in strict
    # mode. Distinguish a real blank page from an unreadable content container.
    content = page.get("/Contents")
    if content is not None:
        content = content.get_object()
        streams = content if isinstance(content, ArrayObject) else (content,)
        if any(not isinstance(stream.get_object(), (StreamObject, NullObject)) for stream in streams):
            raise ValueError("PDF page Contents must contain streams")  # noqa: TRY003
    return page.extract_text() or ""


def _document_id(value: str) -> str:
    if not value.strip():
        raise ValueError("PDF document_id must not be empty or blank")  # noqa: TRY003
    return value


def _page_identity(document_id: str, document_sha256: str, page_number: int) -> str:
    # Structural encoding prevents delimiter collisions; hashing also keeps
    # arbitrarily long document names within the Source identity budget.
    parts = json.dumps(
        (_document_id(document_id), document_sha256, page_number), ensure_ascii=False, separators=(",", ":")
    )
    return "pdf:" + hashlib.sha256(parts.encode("utf-8")).hexdigest()


class _PDFPageAdapter:
    name = "pdf-page"
    input_class = PDFPageInput
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=_page_identity(value.document_id, value.document_sha256, value.page_number), **value.model_dump()
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFPageTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFPageAdapter(), projections=(_PDFPageTextEvidenceProjection(),)
)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "search_pdf_pages",
]
