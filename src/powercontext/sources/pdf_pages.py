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
import re
import zlib
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, Field, field_validator, model_validator
from pypdf import PageObject, PdfReader, get_configuration
from pypdf._font import Font
from pypdf._text_extraction import get_text_operands
from pypdf.filters import decode_stream_data
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    EncodedStreamObject,
    FloatObject,
    NameObject,
    NullObject,
    NumberObject,
    PdfObject,
    StreamObject,
    TextStringObject,
    create_string_object,
)
from typing_extensions import override

from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.pdf_retrieval import search_pdf_pages
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

_DocumentSHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_PhysicalPageNumber = Annotated[int, Field(ge=1, strict=True)]
_TEXT_POSITION_RESETS = frozenset({b"BT", b"ET", b"Tm", b"Td", b"TD", b"T*", b"'", b'"'})
_TEXT_SHOW_OPERATORS = frozenset({b"Tj", b"TJ", b"'", b'"'})


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
        with ExitStack() as restore:
            stream = _prepare_content(
                page.get("/Contents"), page, resources, resources, set(), set(), inherited, restore
            )
            contents = NameObject("/Contents")
            if contents in page:
                restore.callback(page.__setitem__, contents, page.raw_get(contents))
            else:
                restore.callback(page.__delitem__, contents)
            page[contents] = stream
            forms = _FormFontInheritance(page, resources)
            restore.callback(forms.close)
            return page.extract_text(visitor_operand_before=forms.before, visitor_operand_after=forms.after) or ""
    finally:
        # A legacy Form can occur on several pages with different fonts. Keep
        # the repair local to this extraction instead of changing the reader's
        # shared object permanently or rewriting the original document bytes.
        for obj, previous in reversed(inherited):
            if previous is None:
                del obj["/Resources"]
            else:
                obj[NameObject("/Resources")] = previous


class _FormFontInheritance:
    """Expose each invocation's inherited font to pypdf's independent Form extractor."""

    def __init__(self, page: PageObject, resources: DictionaryObject) -> None:
        self.page = page
        self.resources = resources
        self.font: PdfObject | None = None
        self.size = 12.0
        self.graphics: list[tuple[PdfObject | None, float]] = []
        self.invocations: list[ExitStack | None] = []
        self.actual_text = _ActualText(self)

    def before(self, operator: bytes, operands: list[PdfObject], cm: list[float], tm: list[float]) -> None:
        if operator == b"q":
            self.graphics.append((self.font, self.size))
        elif operator == b"Q" and self.graphics:
            self.font, self.size = self.graphics.pop()
        elif operator == b"Tf" and len(operands) >= 2:
            fonts = self.resources.get("/Font", DictionaryObject()).get_object()
            self.font = cast(DictionaryObject, fonts).get(operands[0])
            self.size = float(cast(FloatObject | NumberObject, operands[1]))
        elif operator == b"Do":
            xobjects = cast(DictionaryObject, self.resources["/XObject"])
            form = cast(StreamObject, xobjects[operands[0]].get_object())
            if form.get("/Subtype") == "/Form":
                self._enter(form)
            else:
                self.invocations.append(None)
        self.actual_text.before(operator, operands, cm, tm)

    def _enter(self, form: StreamObject) -> None:
        restore = ExitStack()
        self.invocations.append(restore)
        restore.callback(setattr, self, "resources", self.resources)
        restore.callback(setattr, self, "font", self.font)
        restore.callback(setattr, self, "size", self.size)
        restore.callback(setattr, self, "graphics", self.graphics)
        self.graphics = self.graphics.copy()
        # Preparation has already supplied legacy Forms' missing resources.
        resources = cast(DictionaryObject, form["/Resources"])
        self.resources = resources
        if self.font is None:
            return
        inherited_resources = DictionaryObject(resources)
        fonts = DictionaryObject(cast(DictionaryObject, resources.get("/Font", DictionaryObject()).get_object()))
        alias = NameObject("/PowerContextInheritedFont")
        while alias in fonts:
            alias = NameObject(str(alias) + "_")
        fonts[alias] = self.font
        inherited_resources[NameObject("/Font")] = fonts
        restore.callback(form.__setitem__, NameObject("/Resources"), form.raw_get("/Resources"))
        form[NameObject("/Resources")] = inherited_resources
        self.resources = inherited_resources
        prepared = ContentStream(form, self.page.pdf, "bytes")
        # Tf is a graphics-state parameter retained across BT/ET. Repeating
        # its inherited value is equivalent, including when the Form later
        # selects its own font. A fresh view is needed for each shared Do.
        prepared.operations = [
            ([], b"BT"),
            ([alias, FloatObject(self.size)], b"Tf"),
            ([], b"ET"),
            *prepared.operations,
        ]
        if isinstance(form, EncodedStreamObject):
            restore.callback(setattr, form, "decoded_self", form.decoded_self)
            form.decoded_self = prepared
        else:
            restore.callback(form.set_data, form.get_data())
            form.set_data(prepared.get_data())

    def after(self, operator: bytes, _operands: list[PdfObject], _cm: list[float], _tm: list[float]) -> None:
        self.actual_text.restore_operands()
        if operator == b"Do":
            restore = self.invocations.pop()
            if restore is not None:
                restore.close()

    def close(self) -> None:
        self.actual_text.restore_operands()
        while self.invocations:
            restore = self.invocations.pop()
            if restore is not None:
                restore.close()


class _ActualTextOperand(TextStringObject):
    """Logical replacement text with the original glyphs' width bookkeeping."""

    _glyphs: str

    def __new__(cls, text: str, glyphs: str) -> Self:
        value = super().__new__(cls, text)
        value._glyphs = glyphs
        return value

    @override
    def __iter__(self) -> Iterator[str]:
        # Fixed pypdf 6.19 concatenates a string operand, then iterates it to
        # measure advances. ActualText changes the logical string, not the
        # painted glyph widths. The fallback is used during base construction.
        return iter(getattr(self, "_glyphs", str(self)))


@dataclass
class _ActualTextSpan:
    text: str
    emitted: bool = False


class _ActualText:
    """Apply standard marked-content replacement text without altering state."""

    def __init__(self, forms: _FormFontInheritance) -> None:
        self.forms = forms
        self.spans: list[_ActualTextSpan | None] = []
        self.fonts: dict[int, Font] = {}
        self.modified: list[tuple[list[PdfObject], int, PdfObject]] = []

    def restore_operands(self) -> None:
        for values, index, value in reversed(self.modified):
            values[index] = value
        self.modified.clear()

    def before(self, operator: bytes, operands: list[PdfObject], cm: list[float], tm: list[float]) -> None:
        if operator in (b"BDC", b"BMC"):
            text = self._text(operands[1]) if operator == b"BDC" and len(operands) > 1 else None
            self.spans.append(_ActualTextSpan(text) if text is not None else None)
        elif operator == b"EMC":
            if self.spans:
                self.spans.pop()
        elif operator in _TEXT_SHOW_OPERATORS:
            span = next((span for span in self.spans if span is not None), None)
            if span is not None and operands:
                self._replace(operator, operands, span, cm, tm)

    def _text(self, properties: PdfObject | None) -> str | None:
        if properties is None:
            return None
        properties = properties.get_object()
        if isinstance(properties, NameObject):
            definitions = self.forms.resources.get("/Properties", DictionaryObject()).get_object()
            if not isinstance(definitions, DictionaryObject) or properties not in definitions:
                return None
            properties = definitions[properties].get_object()
        if not isinstance(properties, DictionaryObject):
            return None
        value = properties.get("/ActualText")
        if value is None or isinstance(value.get_object(), NullObject):
            return None
        value = value.get_object()
        if isinstance(value, bytes):
            value = create_string_object(value)
        if not isinstance(value, TextStringObject):
            raise TypeError("PDF ActualText must be a text string")  # noqa: TRY003
        return str(value)

    def _replace(
        self, operator: bytes, operands: list[PdfObject], span: _ActualTextSpan, cm: list[float], tm: list[float]
    ) -> None:
        if operator == b"TJ":
            if not isinstance(operands[0], ArrayObject):
                return
            values = operands[0]
            indices = range(len(values))
        else:
            values = operands
            indices = range(2, min(3, len(values))) if operator == b'"' else range(min(1, len(values)))
        for index in indices:
            value = values[index]
            if operator == b"TJ" and isinstance(value, (NumberObject, FloatObject)):
                # Fixed pypdf 6.19 uses TJ adjustments only to infer spaces;
                # it does not apply them to its text matrix. ActualText
                # already supplies all logical whitespace for this span.
                self.modified.append((values, index, value))
                values[index] = NumberObject(0)
                continue
            if not isinstance(value, (bytes, TextStringObject)) or not value:
                continue
            glyphs = self._glyphs(value, cm, tm)
            self.modified.append((values, index, value))
            values[index] = _ActualTextOperand("" if span.emitted else span.text, glyphs)
            span.emitted = True

    def _glyphs(self, value: bytes | TextStringObject, cm: list[float], tm: list[float]) -> str:
        reference = self.forms.font
        if reference is None:
            return value.decode("charmap") if isinstance(value, bytes) else str(value)
        source = cast(DictionaryObject, reference.get_object())
        font = self.fonts.get(id(source))
        if font is None:
            font = Font.from_font_resource(source)
            self.fonts[id(source)] = font
        # This fixed-pypdf helper accepts byte operands despite its str hint.
        glyphs, _ = get_text_operands([cast(str, value)], cm, tm, font, (0, 90, 180, 270))
        return glyphs


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


def _prepare_content(
    content: PdfObject | None,
    page: PageObject,
    resources: DictionaryObject,
    page_resources: DictionaryObject,
    ancestors: set[int],
    validated_forms: set[int],
    inherited: list[tuple[DictionaryObject, PdfObject | None]],
    restore: ExitStack,
) -> ContentStream:
    fonts = resources.get("/Font")
    if fonts is not None and isinstance(fonts.get_object(), NullObject):
        restore.callback(resources.__setitem__, NameObject("/Font"), resources.raw_get("/Font"))
        del resources["/Font"]
    stream = _content_stream(content, page)
    for operands, operator in stream.operations:
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
        prepared = _prepare_content(
            xobject,
            page,
            _bind_resources(xobject, page_resources, inherited),
            page_resources,
            ancestors | {identity},
            validated_forms,
            inherited,
            restore,
        )
        # Keep the original encoded bytes and restore the extraction view after
        # this page. A shared Form can be invoked again under another page's
        # resources, and filtered streams must retain their original filters.
        if isinstance(xobject, EncodedStreamObject):
            restore.callback(setattr, xobject, "decoded_self", xobject.decoded_self)
            xobject.decoded_self = prepared
        else:
            restore.callback(xobject.set_data, xobject.get_data())
            xobject.set_data(prepared.get_data())
        validated_forms.add(identity)
    _normalize_text_positions(stream)
    return stream


def _discard_unused_text_adjustments(stream: ContentStream) -> None:
    operations = list(stream.operations)
    reset_before_next_show = True
    for index in range(len(operations) - 1, -1, -1):
        operands, operator = operations[index]
        if operator == b"TJ" and reset_before_next_show and operands and isinstance(operands[0], ArrayObject):
            values = list(operands[0])
            # A terminal adjustment whose text matrix is never reused has no
            # rendered effect, but pypdf infers a word gap from it anyway.
            while values and isinstance(values[-1], (int, float)):
                values.pop()
            operations[index] = ([ArrayObject(values)], operator)
        if operator in _TEXT_POSITION_RESETS:
            reset_before_next_show = True
        elif operator in _TEXT_SHOW_OPERATORS:
            reset_before_next_show = False
    stream.operations = operations


def _normalize_text_positions(stream: ContentStream) -> None:
    _discard_unused_text_adjustments(stream)
    normalized = []
    needs_initial_position = False
    for operands, operator in stream.operations:
        if operator == b"BT":
            needs_initial_position = True
        elif operator in _TEXT_POSITION_RESETS:
            needs_initial_position = False
        elif operator in _TEXT_SHOW_OPERATORS:
            if needs_initial_position:
                # BT already sets both text matrices to identity. Make that
                # reset visible before the first glyph so pypdf consumes a
                # changed CTM before displaying a partial word from a TJ array.
                normalized.append(([NumberObject(value) for value in (1, 0, 0, 1, 0, 0)], b"Tm"))
            needs_initial_position = False
        normalized.append((operands, operator))
    stream.operations = normalized


def _content_stream(content: PdfObject | None, page: PageObject) -> ContentStream:
    # pypdf treats a malformed /Contents scalar as an empty page even in strict
    # mode. Its filter decoders can also recover unterminated or partial
    # streams, and its text extractor suppresses errors in nested Forms.
    # Validate these evidence-bearing streams before accepting a whole page.
    if content is None:
        return ContentStream(None, page.pdf, "bytes")
    content = content.get_object()
    if content is None or isinstance(content, NullObject):
        return ContentStream(None, page.pdf, "bytes")
    streams = content if isinstance(content, ArrayObject) else (content,)
    for reference in streams:
        stream = reference.get_object()
        if isinstance(stream, NullObject):
            continue
        if not isinstance(stream, StreamObject):
            raise TypeError("PDF page Contents must contain streams")  # noqa: TRY003
        _validate_encoded_stream(stream)

    return ContentStream(content, page.pdf, "bytes")


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
        if filter_name not in (
            "/FlateDecode",
            "/Fl",
            "/RunLengthDecode",
            "/RL",
            "/ASCII85Decode",
            "/A85",
            "/ASCIIHexDecode",
            "/AHx",
        ):
            continue
        # Decode any preceding layers through pypdf, including their predictor
        # parameters. Check each encoded layer before its recovery fallback.
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
        elif filter_name in ("/ASCII85Decode", "/A85", "/ASCIIHexDecode", "/AHx"):
            _validate_ascii_end_marker(data, base85=filter_name in ("/ASCII85Decode", "/A85"))
        else:
            _validate_deflate(data)


def _validate_ascii_end_marker(data: bytes, *, base85: bool) -> None:
    # ISO 32000-1, 7.4.2-3 require > or ~>; whitespace is ignored. Do not
    # require the marker to occupy the final bytes of the stream.
    complete = re.search(rb"~[\x00\t\n\f\r ]*>", data) is not None if base85 else b">" in data
    if not complete:
        raise ValueError("PDF ASCII-encoded content is missing its end marker")  # noqa: TRY003


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
