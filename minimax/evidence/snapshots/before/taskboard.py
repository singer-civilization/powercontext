from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict


class Task(TypedDict):
    id: int
    title: str
    status: str


def list_tasks(tasks: Iterable[Task]) -> list[Task]:
    """Return every task as required by specification v1."""
    return list(tasks)
