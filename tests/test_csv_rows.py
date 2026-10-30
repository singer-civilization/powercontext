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

"""CSV identity, field fidelity, and validation at the public source boundary."""

from __future__ import annotations

import asyncio
import json

import pytest

from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)


def test_distinct_space_padded_columns_and_keys_remain_exact() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="rows:中文",
            primary_key=" id ",
            content="id, id ,值,empty\nfirst, key ,001,\nsecond,key ,true,\n".encode(),
        )
    )
    assert [row.primary_key_value for row in rows] == [" key ", "key "]
    assert rows[0].columns == ("id", " id ", "值", "empty")
    assert rows[0].values == ("first", " key ", "001", "")
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(rows[0]))
    assert isinstance(source, CSVRowSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.content == '{"id":"first"," id ":" key ","值":"001","empty":""}'
    assert list(evidence.metadata) == ["document_id", "primary_key", "primary_key_value", "columns", "row"]
    assert list(json.loads(evidence.content)) == ["id", " id ", "值", "empty"]
    assert source.name == source.source_id == "csv:rows%3A%E4%B8%AD%E6%96%87:%20key%20"


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbf",
        b"\nid,name\n1,name\n",
        b"id,name\n1,valid\n   ,blank\n",
        b"id,name\n1,valid\n2,too,many\n",
        b'id,name\n1,valid\n2,"closed"suffix\n',
        b'id,name\n1,valid\n2,"unterminated\n',
        b"id,name\n1,valid\n\xc0\xaf,invalid\n",
        b'id,name\n1,valid\n""\n',
        b"id,name\n1,valid\n \n",
    ],
)
def test_invalid_documents_never_return_a_valid_prefix(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="document", primary_key="id", content=content))


@pytest.mark.parametrize("document_id", ["", " ", "\tdoc", "doc\n", "\u3000doc"])
def test_document_identity_rejects_all_surrounding_whitespace(document_id: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key="id", content=b"id\n"))
    with pytest.raises(ValueError):
        csv_row_source_id(document_id, "key")


@pytest.mark.parametrize("primary_key", ["", "id", " id", "id "])
def test_primary_key_column_requires_an_exact_match(primary_key: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(
            CSVDocumentInput(document_id="document", primary_key=primary_key, content=b" id ,name\n1,name\n")
        )


def test_empty_physical_records_and_quoted_empty_fields_have_different_meanings() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="document",
            primary_key="id",
            content=b'\xef\xbb\xbfid,note\r\n\r\n1,""\r\n\r\n2,"first\r\n\r\nlast"\r\n',
        )
    )
    assert [row.values for row in rows] == [("1", ""), ("2", "first\r\n\r\nlast")]
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="document", primary_key="id", content=b'id\n\n""\n'))


def test_percent_encoding_is_injective_and_length_is_checked_after_encoding() -> None:
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("doc", "a/b") != csv_row_source_id("doc", "a%2Fb")
    assert len(csv_row_source_id("doc", "k" * 248)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("doc", "k" * 249)
    with pytest.raises(ValueError):
        csv_row_source_id("doc", "中" * 28)
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content=b"id\n" + b"k" * 249 + b"\n"))


@pytest.mark.parametrize(
    "changes",
    [
        {"primary_key_value": "wrong"},
        {"primary_key_value": " "},
        {"columns": ("id", "id")},
        {"columns": ("id", " ")},
        {"columns": ()},
        {"values": ("key",)},
        {"primary_key": "missing"},
        {"document_id": " doc"},
    ],
)
def test_direct_row_inputs_cannot_bypass_csv_invariants(changes: dict[str, object]) -> None:
    payload = {
        "document_id": "doc",
        "primary_key": "id",
        "primary_key_value": "key",
        "columns": ("id", "note"),
        "values": ("key", "value"),
    }
    payload.update(changes)
    with pytest.raises(ValueError):
        CSVRowInput.model_validate(payload)


def test_json_round_trip_retains_captured_row_and_transport_projection() -> None:
    row = parse_csv_document(
        CSVDocumentInput(
            document_id="document",
            primary_key="id",
            content='id,note,number,boolean,null\n01,"a,b\n中文",001,false,null\n'.encode(),
        )
    )[0]
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    restored = CSVRowSource.model_validate_json(source.model_dump_json())
    assert asyncio.run(registry.read(restored)) == row
    manifest = manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)
    observation = project_source_for_transport(registry, restored)
    assert observation.definition_fingerprint == manifest.fingerprint
    assert observation.payload["values"] == ["01", "a,b\n中文", "001", "false", "null"]
    assert observation.projection(TEXT_EVIDENCE_PROJECTION_KEY) == registry.project(
        source, TEXT_EVIDENCE_PROJECTION_KEY
    )
    payload = restored.model_dump()
    payload["source_id"] = "csv:document:different"
    with pytest.raises(ValueError):
        CSVRowSource.model_validate(payload)
