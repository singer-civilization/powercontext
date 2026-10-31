# SDD Engineering Context

## Objective
Implement the v2 task status filter and make done-filter tests pass

## Applicable specification

- [specs/task-filter.md:3#9c06e3f46313] version=2 score=23: `list_tasks(tasks, status="all")` returns tasks in their original order.

```markdown
# Task list specification v2

`list_tasks(tasks, status="all")` returns tasks in their original order.

- `status="all"` returns every task.
- `status="open"` returns only open tasks.
- `status="done"` returns only completed tasks.
- Any other status raises `ValueError` with a message that names the unsupported value.

The v2 status filter supersedes v1. Existing callers that omit `status` remain compatible.
```

## Code and symbols

- [sample_project/src/taskboard.py:13#a6c931660893] version=2 symbols=Task,list_tasks score=11: def list_tasks(tasks: Iterable[Task]) -> list[Task]:

```python
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
```

## Tests

- [sample_project/tests/test_taskboard.py:12#a68555da6aad] version=2 symbols=test_default_keeps_all_tasks,test_filters_done_tasks,test_rejects_unknown_status score=28: assert list_tasks(TASKS, status="done") == [TASKS[0]]

```python
import pytest
from taskboard import list_tasks

TASKS = [
    {"id": 1, "title": "write spec", "status": "done"},
    {"id": 2, "title": "implement filter", "status": "open"},
    {"id": 3, "title": "run validation", "status": "open"},
]


def test_filters_done_tasks() -> None:
    assert list_tasks(TASKS, status="done") == [TASKS[0]]


def test_default_keeps_all_tasks() -> None:
    assert list_tasks(TASKS) == TASKS


def test_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="blocked"):
        list_tasks(TASKS, status="blocked")
```

## Persisted progress (PowerContext)

- task_id: task-filter
- updated_at: 2026-10-31T06:43:41.217634+00:00
- completed: indexed v1 specification, code, and target tests, refreshed the index with specification v2
- todo: generate the implementation patch with MiniMax, run the target and regression tests
- validation.before: target test fails because list_tasks has no status parameter

