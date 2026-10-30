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

"""Self-contained, verified UTF-8 context packs and prefix-reuse deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ContextPackError(ValueError):
    """A context pack cannot be safely decoded, chunked, or reconstructed."""


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("context items must be a tuple of bytes")  # noqa: TRY003
    try:
        for item in items:
            item.decode("utf-8")
    except UnicodeError as error:
        raise ContextPackError("context chunks must each be valid UTF-8") from error  # noqa: TRY003


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("wire chunks must be a Base64 string array")  # noqa: TRY003
    try:
        decoded = []
        for item in value:
            if not isinstance(item, str):
                raise ContextPackError("wire chunks must be Base64 strings")  # noqa: TRY003
            decoded.append(base64.b64decode(item, validate=True))
        result = tuple(decoded)
    except (ValueError, binascii.Error) as error:
        raise ContextPackError("invalid Base64 context chunk") from error  # noqa: TRY003
    if _encode_items(result) != value:
        raise ContextPackError("context chunks must use standard canonical Base64")  # noqa: TRY003
    _validate_items(result)
    return result


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError("duplicate context wire field")  # noqa: TRY003
        result[key] = value
    return result


def _decode(data: bytes, schema: str, fields: set[str]) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
    except (ValueError, TypeError, AttributeError, RecursionError) as error:
        raise ContextPackError("invalid context wire JSON") from error  # noqa: TRY003
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != schema:
        raise ContextPackError("unexpected context wire fields or schema")  # noqa: TRY003
    return value


def _validate_digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContextPackError("invalid context digest")  # noqa: TRY003
    return value


@dataclass(frozen=True)
class ContextPack:
    """A complete ordered UTF-8 context, with independently verifiable wire size."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        _validate_items(items)
        payload = {"schema": _PACK_SCHEMA, "items": _encode_items(items)}
        digest = "sha256:" + hashlib.sha256(_json(payload)).hexdigest()
        wire = {**payload, "digest": digest, "byte_count": 0}
        while wire["byte_count"] != len(_json(wire)):
            wire["byte_count"] = len(_json(wire))
        return cls(items, digest, wire["byte_count"])

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        if content is None:
            return cls.build(())
        if not isinstance(content, str):
            raise ContextPackError("context content must be text or None")  # noqa: TRY003
        chunks: list[bytes] = []
        current = bytearray()
        try:
            for character in content:
                encoded = character.encode("utf-8")
                if len(encoded) > chunk_bytes:
                    raise ContextPackError("chunk limit cannot contain a UTF-8 character")  # noqa: TRY003
                if len(current) + len(encoded) > chunk_bytes:
                    chunks.append(bytes(current))
                    current.clear()
                current.extend(encoded)
        except UnicodeError as error:
            raise ContextPackError("context content is not valid UTF-8") from error  # noqa: TRY003
        if current:
            chunks.append(bytes(current))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        return _json({
            "schema": _PACK_SCHEMA,
            "items": _encode_items(self.items),
            "digest": self.digest,
            "byte_count": self.byte_count,
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _decode(data, _PACK_SCHEMA, {"schema", "items", "digest", "byte_count"})
        items = _decode_items(payload["items"])
        result = cls.build(items)
        if (
            type(payload["byte_count"]) is not int
            or payload["byte_count"] != result.byte_count
            or payload["digest"] != result.digest
        ):
            raise ContextPackError("context pack digest or byte count mismatch")  # noqa: TRY003
        return result


@dataclass(frozen=True)
class ContextPackDelta:
    """Reuse a verified base prefix and append the target's complete suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a nonnegative integer")  # noqa: TRY003
        _validate_items(self.appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        base = ContextPack.from_bytes(base.to_bytes())
        target = ContextPack.from_bytes(target.to_bytes())
        prefix = 0
        for old, new in zip(base.items, target.items, strict=False):
            if old != new:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    def to_bytes(self) -> bytes:
        return _json({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        })

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _decode(data, _DELTA_SCHEMA, {"schema", "base_digest", "target_digest", "prefix_items", "appended"})
        prefix = payload["prefix_items"]
        if type(prefix) is not int:
            raise ContextPackError("prefix_items must be an integer")  # noqa: TRY003
        return cls(
            base_digest=_validate_digest(payload["base_digest"]),
            target_digest=_validate_digest(payload["target_digest"]),
            prefix_items=prefix,
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Reconstruct solely from the verified base and the received delta."""

    base = ContextPack.from_bytes(base.to_bytes())
    if base.digest != delta.base_digest:
        raise ContextPackError("context delta base digest mismatch")  # noqa: TRY003
    if type(delta.prefix_items) is not int or not 0 <= delta.prefix_items <= len(base.items):
        raise ContextPackError("context delta prefix is outside the base")  # noqa: TRY003
    result = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if result.digest != delta.target_digest:
        raise ContextPackError("context delta target digest mismatch")  # noqa: TRY003
    return result


__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]
