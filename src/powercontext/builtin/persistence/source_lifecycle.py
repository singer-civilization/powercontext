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

"""Durable scope-local evidence dependencies and generation eligibility."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE


async def load_lifecycle(connection: AsyncConnection, scope_id: str) -> dict[str, Any]:
    value = await connection.scalar(
        select(SOURCE_LIFECYCLE_TABLE.c.payload).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
    )
    return {} if value is None else json.loads(value)


async def save_lifecycle(connection: AsyncConnection, scope_id: str, nodes: dict[str, Any]) -> None:
    table = SOURCE_LIFECYCLE_TABLE
    payload = json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))
    existing = await connection.scalar(select(table.c.scope_id).where(table.c.scope_id == scope_id))
    if existing is None:
        await connection.execute(insert(table).values(scope_id=scope_id, payload=payload))
    else:
        await connection.execute(update(table).where(table.c.scope_id == scope_id).values(payload=payload))


async def unavailable_topics(connection: AsyncConnection, scope_id: str) -> frozenset[tuple[str, int]]:
    nodes = await load_lifecycle(connection, scope_id)
    return frozenset(
        (node["reference"]["artifact_id"], node["reference"]["revision"])
        for node in nodes.values()
        if node["kind"] == "topic" and not node["available"]
    )
