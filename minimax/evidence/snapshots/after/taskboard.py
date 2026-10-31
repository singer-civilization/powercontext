from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict


class Task(TypedDict):
    id: int
    title: str
    status: str


VALID_STATUSES = {"all", "open", "done"}


def list_tasks(tasks: Iterable[Task], status: str = "all") -> list[Task]:
    """Return tasks selected by the v2 status filter."""
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported task status: {status}")
    items = list(tasks)
    if status == "all":
        return items
    return [task for task in items if task["status"] == status]
