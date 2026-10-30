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
import zlib
from collections.abc import Iterator
from typing import Annotated, Literal, cast

from pydantic import BaseModel, Field, field_validator, model_validator
from pypdf import PageObject, PdfReader, get_configuration
from pypdf.filters import decode_stream_data
from pypdf.generic import ArrayObject, ContentStream, DictionaryObject, NameObject, NullObject, PdfObject, StreamObject

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
    resources = _page_resources(page)
    inherited: list[tuple[DictionaryObject, PdfObject | None]] = []
    try:
        _bind_resources(page, resources, inherited)
        _validate_content(page.get("/Contents"), page, resources, resources, set(), set(), inherited)
        return page.extract_text() or ""
    finally:
        # A legacy Form can occur on several pages with different fonts. Keep
        # the repair local to this extraction instead of changing the reader's
        # shared object permanently or rewriting the original document bytes.
        for obj, previous in reversed(inherited):
            if previous is None:
                del obj["/Resources"]
            else:
                obj[NameObject("/Resources")] = previous


def _page_resources(page: PageObject) -> DictionaryObject:
    current: DictionaryObject = page
    visited: set[int] = set()
    while True:
        if id(current) in visited:
            raise ValueError("PDF page resource inheritance must not be cyclic")  # noqa: TRY003
        visited.add(id(current))
        resources = current.get("/Resources")
        if resources is not None:
            resources = resources.get_object()
            if not isinstance(resources, NullObject):
                if not isinstance(resources, DictionaryObject):
                    raise TypeError("PDF Resources must be a dictionary")  # noqa: TRY003
                return resources
        parent = current.get("/Parent")
        if parent is None:
            return DictionaryObject()
        current = cast(DictionaryObject, parent.get_object())


def _bind_resources(
    obj: DictionaryObject,
    default: DictionaryObject,
    inherited: list[tuple[DictionaryObject, PdfObject | None]],
) -> DictionaryObject:
    resources = obj.get("/Resources")
    if resources is not None:
        resources = resources.get_object()
    if resources is None or isinstance(resources, NullObject):
        # ISO 32000-1, 7.8.3: legacy Forms without Resources use the page's
        # resources, including when nested inside an independent Form. pypdf's
        # text extractor does not implement that fallback itself. A null value
        # is equivalent to an absent dictionary entry (7.3.7).
        inherited.append((obj, obj.raw_get("/Resources") if "/Resources" in obj else None))
        obj[NameObject("/Resources")] = default
        return default
    if not isinstance(resources, DictionaryObject):
        raise TypeError("PDF Resources must be a dictionary")  # noqa: TRY003
    return resources


def _validate_content(
    content: PdfObject | None,
    page: PageObject,
    resources: DictionaryObject,
    page_resources: DictionaryObject,
    ancestors: set[int],
    validated_forms: set[int],
    inherited: list[tuple[DictionaryObject, PdfObject | None]],
) -> None:
    for operands, operator in _content_operations(content, page):
        if operator != b"Do":
            continue
        xobjects = cast(DictionaryObject, resources["/XObject"].get_object())
        xobject = cast(StreamObject, xobjects[operands[0]].get_object())
        if xobject.get("/Subtype") != "/Form":
            continue
        identity = id(xobject)
        if identity in ancestors:
            raise ValueError("PDF Form content must not be cyclic")  # noqa: TRY003
        if identity in validated_forms:
            continue
        _validate_content(
            xobject,
            page,
            _bind_resources(xobject, page_resources, inherited),
            page_resources,
            ancestors | {identity},
            validated_forms,
            inherited,
        )
        validated_forms.add(identity)


def _content_operations(content: PdfObject | None, page: PageObject) -> Iterator[tuple[list[PdfObject], bytes]]:
    # pypdf treats a malformed /Contents scalar as an empty page even in strict
    # mode. Its Flate and RunLength decoders can also recover only part of a
    # damaged stream, and its text extractor suppresses errors in nested Forms.
    # Validate these evidence-bearing streams before accepting a whole page.
    if content is None:
        return
    content = content.get_object()
    if content is None or isinstance(content, NullObject):
        return
    streams = content if isinstance(content, ArrayObject) else (content,)
    for reference in streams:
        stream = reference.get_object()
        if isinstance(stream, NullObject):
            continue
        if not isinstance(stream, StreamObject):
            raise TypeError("PDF page Contents must contain streams")  # noqa: TRY003
        _validate_encoded_stream(stream)

    yield from ContentStream(content, page.pdf).operations


def _validate_encoded_stream(stream: StreamObject) -> None:
    filters = stream.get("/Filter", ArrayObject()).get_object()
    if isinstance(filters, NullObject):
        return
    if not isinstance(filters, ArrayObject):
        filters = ArrayObject([filters])
    parameters = stream.get("/DecodeParms")
    if parameters is not None:
        parameters = parameters.get_object()
    for index, filter_name in enumerate(filters):
        filter_name = filter_name.get_object()
        if filter_name not in ("/FlateDecode", "/Fl", "/RunLengthDecode", "/RL"):
            continue
        # Decode any preceding layers through pypdf, including their predictor
        # parameters. Check the compressed bytes before its recovery fallback.
        prefix = StreamObject()
        prefix._data = stream._data  # pypdf exposes no raw stream-data accessor.
        prefix[NameObject("/Filter")] = ArrayObject(filters[:index])
        if isinstance(parameters, ArrayObject):
            prefix[NameObject("/DecodeParms")] = ArrayObject(parameters[:index])
        elif parameters is not None:
            prefix[NameObject("/DecodeParms")] = parameters
        data = decode_stream_data(prefix)
        if filter_name in ("/RunLengthDecode", "/RL"):
            _validate_run_length(data)
        else:
            _validate_deflate(data)


def _validate_run_length(data: bytes) -> None:
    position = 0
    output_length = 0
    output_limit = get_configuration().run_length_maximum_output_length
    # Walk encoded runs and count output bytes without constructing another
    # decompressed copy. A literal byte equal to 128 is not an EOD marker.
    while position < len(data):
        control = data[position]
        position += 1
        if control == 128:
            return
        if control < 128:
            length = control + 1
            position += length
        else:
            length = 257 - control
            position += 1
        if position > len(data):
            raise ValueError("PDF RunLength content has an incomplete run")  # noqa: TRY003
        output_length += length
        if output_length > output_limit:
            raise ValueError("PDF RunLength content exceeds the parser limit")  # noqa: TRY003
    raise ValueError("PDF RunLength content is missing its end marker")  # noqa: TRY003


def _validate_deflate(data: bytes) -> None:
    decoder = zlib.decompressobj()
    output_limit = get_configuration().zlib_maximum_output_length
    output_length = 0
    # Discard decoded chunks: this check must not allocate an unbounded second
    # copy of the page's content stream before pypdf extracts its text.
    for start in range(0, len(data), 65_536):
        pending = data[start : start + 65_536]
        while pending:
            output_length += len(decoder.decompress(pending, 65_536))
            if output_limit and output_length > output_limit:
                raise ValueError("PDF compressed content exceeds the parser limit")  # noqa: TRY003
            if decoder.eof:
                return
            pending = decoder.unconsumed_tail
    raise ValueError("PDF compressed content is incomplete")  # noqa: TRY003


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
