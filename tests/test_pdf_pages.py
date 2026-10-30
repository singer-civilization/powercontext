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

"""Complete PDF extraction and revision-specific source evidence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import zlib
from typing import cast

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NullObject,
    NumberObject,
    TextStringObject,
)

from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    PDFPageSource,
    parse_pdf_document,
)


def _pdf(texts: tuple[str | None, ...], *, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        if text is None:
            continue
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 10 100 Td ({escaped}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner-secret")  # noqa: S106 - Synthetic fixture.
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _broken_later_page() -> bytes:
    writer = PdfWriter(clone_from=io.BytesIO(_pdf(("valid first page", "invalid second page"))))
    writer.pages[1][NameObject("/Contents")] = TextStringObject("not a content stream")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _filtered_later_page(
    content: bytes,
    filters: tuple[str, ...] = ("/FlateDecode",),
    *,
    form_depth: int = 0,
    split_streams: bool = False,
) -> bytes:
    writer = PdfWriter(clone_from=io.BytesIO(_pdf(("Valid first page", "original second page"))))
    page = writer.pages[1]
    resources = page["/Resources"]
    stream = EncodedStreamObject()
    stream._data = content
    stream[NameObject("/Filter")] = ArrayObject([NameObject(name) for name in filters])
    stream[NameObject("/DecodeParms")] = ArrayObject([NullObject() for _ in filters])
    for depth in range(form_depth):
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/BBox")] = ArrayObject([
            NumberObject(0),
            NumberObject(0),
            NumberObject(300),
            NumberObject(300),
        ])
        stream[NameObject("/Resources")] = resources
        name = NameObject(f"/Form{depth}")
        resources = DictionaryObject({NameObject("/XObject"): DictionaryObject({name: writer._add_object(stream)})})
        stream = DecodedStreamObject()
        stream.set_data(f"{name} Do".encode())
    page[NameObject("/Resources")] = resources
    if split_streams:
        prefix = DecodedStreamObject()
        prefix.set_data(b"q\n")
        suffix = DecodedStreamObject()
        suffix.set_data(b"\nQ")
        page[NameObject("/Contents")] = ArrayObject([
            writer._add_object(prefix),
            writer._add_object(stream),
            writer._add_object(suffix),
        ])
    else:
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_all_physical_pages_keep_text_numbers_blank_positions_and_original_digest() -> None:
    content = _pdf(("Revenue 1,234.56 in 2022", None, "Physical page three (appendix 99)"))
    pages = parse_pdf_document(PDFDocumentInput(document_id=" Annual report ", content=content))
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [page.text for page in pages] == ["Revenue 1,234.56 in 2022", "", "Physical page three (appendix 99)"]
    assert all(page.document_id == " Annual report " for page in pages)
    assert all(page.document_sha256 == hashlib.sha256(content).hexdigest() for page in pages)


def test_captured_source_read_projection_and_transport_preserve_one_exact_page() -> None:
    page = parse_pdf_document(PDFDocumentInput(document_id="财报", content=_pdf(("Cash flow 001.00",))))[0]
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(page))
    assert isinstance(source, PDFPageSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    assert asyncio.run(registry.read(source)) == page
    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence == TextEvidence(
        source_type="pdf-page",
        source_id=source.name,
        content="财报\nCash flow 001.00",
        metadata={"document_id": "财报", "document_sha256": page.document_sha256, "page_number": 1},
    )
    observation = project_source_for_transport(registry, source)
    assert observation.definition_fingerprint == manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION).fingerprint
    assert observation.projection(TEXT_EVIDENCE_PROJECTION_KEY) == evidence.model_dump(mode="json")
    restored = PDFPageSource.model_validate_json(source.model_dump_json())
    assert asyncio.run(registry.read(restored)) == page
    with pytest.raises(ValueError):
        PDFPageSource.model_validate({**source.model_dump(), "page_number": 2})


def test_identity_distinguishes_document_revision_and_physical_page_and_bounds_long_document_ids() -> None:
    first = parse_pdf_document(PDFDocumentInput(document_id="a:b", content=_pdf((None, None))))
    revised = parse_pdf_document(PDFDocumentInput(document_id="a:b", content=_pdf((None, None, None))))
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    inputs = (
        first[0],
        first[1],
        revised[0],
        first[0].model_copy(update={"document_id": "a"}),
        first[0].model_copy(update={"document_id": "财报:" * 1000}),
    )
    sources = tuple(asyncio.run(registry.resolve(page)) for page in inputs)
    assert len({source.name for source in sources}) == len(inputs)
    assert all(len(source.name) <= 256 for source in sources)
    assert asyncio.run(registry.resolve(first[0])).name == sources[0].name
    assert asyncio.run(registry.read(sources[0])) == first[0]


def test_empty_password_permissions_are_readable_but_non_empty_passwords_are_rejected() -> None:
    content = _pdf(("Readable original 123", None), password="")
    pages = parse_pdf_document(PDFDocumentInput(document_id="permissions", content=content))
    assert [page.text for page in pages] == ["Readable original 123", ""]
    assert all(page.document_sha256 == hashlib.sha256(content).hexdigest() for page in pages)
    with pytest.raises(ValueError):
        parse_pdf_document(
            PDFDocumentInput(document_id="secret", content=_pdf(("Secret",), password="required"))  # noqa: S106
        )


@pytest.mark.parametrize(
    "content", [b"", b"not a PDF", b"%PDF-1.7\ntruncated", _pdf(("first", None))[:-40], _broken_later_page()]
)
def test_incomplete_or_invalid_documents_fail_without_partial_pages(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="corrupt", content=content))


@pytest.mark.parametrize("form_depth", [0, 2])
@pytest.mark.parametrize("broken", [b"not a zlib stream", zlib.compress(b"BT /F1 12 Tf (Partial evidence) Tj ET")[:-4]])
def test_damaged_compressed_later_page_fails_atomically(broken: bytes, form_depth: int) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(
            PDFDocumentInput(
                document_id="corrupt-compression", content=_filtered_later_page(broken, form_depth=form_depth)
            )
        )


def test_invalid_form_content_is_not_silently_omitted_from_an_accepted_document() -> None:
    content = _filtered_later_page(zlib.compress(b"BT /F1 12 Tf (unterminated literal"), form_depth=2)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="corrupt-form", content=content))


@pytest.mark.parametrize("form_depth", [0, 1, 2])
@pytest.mark.parametrize("split_streams", [False, True])
def test_complete_filtered_content_preserves_text_through_stream_arrays_and_forms(
    form_depth: int, split_streams: bool
) -> None:
    raw = b"BT /F1 12 Tf 10 100 Td (Revenue 123 and operating income 45) Tj ET"
    content = _filtered_later_page(
        base64.a85encode(zlib.compress(raw), adobe=True),
        ("/ASCII85Decode", "/FlateDecode"),
        form_depth=form_depth,
        split_streams=split_streams,
    )
    pages = parse_pdf_document(PDFDocumentInput(document_id="complete", content=content))
    assert len(pages) == 2
    assert pages[0].text == "Valid first page"
    assert pages[1].text.strip() == "Revenue 123 and operating income 45"


@pytest.mark.parametrize("inherited", [False, True])
def test_form_text_with_indirect_or_inherited_page_resources_is_preserved(inherited: bool) -> None:
    raw = b"BT /F1 12 Tf 10 100 Td (Revenue 123 and operating income 45) Tj ET"
    writer = PdfWriter(clone_from=io.BytesIO(_filtered_later_page(zlib.compress(raw), form_depth=2)))
    page = writer.pages[1]

    def make_indirect_resources(obj: DictionaryObject) -> None:
        resources = cast(DictionaryObject, obj["/Resources"])
        xobjects = cast(DictionaryObject, resources.get("/XObject", DictionaryObject()))
        for form in xobjects.values():
            make_indirect_resources(cast(DictionaryObject, form.get_object()))
        obj[NameObject("/Resources")] = writer._add_object(resources)

    make_indirect_resources(page)
    if inherited:
        cast(DictionaryObject, page["/Parent"])[NameObject("/Resources")] = page.raw_get("/Resources")
        del page["/Resources"]
    output = io.BytesIO()
    writer.write(output)
    pages = parse_pdf_document(PDFDocumentInput(document_id="indirect-resources", content=output.getvalue()))
    assert pages[1].text.strip() == "Revenue 123 and operating income 45"


@pytest.mark.parametrize("document_id", ["", " ", "\n\t", "\u3000"])
def test_document_ids_must_contain_non_whitespace(document_id: str) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id=document_id, content=_pdf((None,))))


@pytest.mark.parametrize(
    "changes", [{"document_sha256": "a" * 63}, {"document_sha256": "A" * 64}, {"page_number": 0}, {"page_number": True}]
)
def test_direct_page_inputs_enforce_revision_and_physical_page_invariants(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PDFPageInput.model_validate({
            "document_id": "report",
            "document_sha256": "a" * 64,
            "page_number": 1,
            "text": "",
            **changes,
        })
