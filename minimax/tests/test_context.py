from sdd_context.context import render_context
from sdd_context.models import SearchHit, TaskState


def test_context_contains_sources_symbols_tests_and_progress() -> None:
    hits = [
        SearchHit("spec", "specs/task-filter.md", "", "a" * 64, 2, 5, "status done", 5),
        SearchHit("code", "sample_project/src/taskboard.py", "list_tasks", "b" * 64, 1, 4, "def list_tasks", 15),
        SearchHit(
            "test", "sample_project/tests/test_taskboard.py", "test_filters_done_tasks", "c" * 64, 1, 3, "done", 11
        ),
    ]
    state = TaskState("task-filter", "filter", ["indexed"], ["implement"], {"before": "failed"})

    result = render_context("implement done filter", hits, state)

    assert "specs/task-filter.md:5#" in result
    assert "symbols=list_tasks" in result
    assert "test_filters_done_tasks" in result
    assert "completed: indexed" in result
    assert "validation.before: failed" in result
