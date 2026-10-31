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
