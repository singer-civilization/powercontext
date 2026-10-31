# SDD Engineering Context

## Objective

Implement the v2 task status filter and make done-filter tests pass

## Applicable specification

- `[specs/task-filter.md:3#d5ff3404eae1]` version=1 score=23: `list_tasks(tasks, status="all")` returns tasks in their original order.

## Code and symbols

- `[sample_project/src/taskboard.py:16#320e263d847e]` version=1 symbols=Task,list_tasks score=28: `def list_tasks(tasks: Iterable[Task], status: str = "all") -> list[Task]:`

## Tests

- `[sample_project/tests/test_taskboard.py:12#92b5cd9f0254]` version=1 symbols=test_default_keeps_all_tasks,test_filters_done_tasks,test_rejects_unknown_status score=28: `assert list_tasks(TASKS, status="done") == [TASKS[0]]`

## Persisted progress (PowerContext)

- task_id: task-filter
- completed: indexed v1 specification, code, and target tests; refreshed the index with specification v2
- todo: generate the implementation patch with MiniMax; run the target and regression tests
- validation.before: target test fails because list_tasks has no status parameter

This sample was emitted by the CLI from the real embedded seekdb and a local PowerContext server. The shortened SHA-256 values match the indexed files.
