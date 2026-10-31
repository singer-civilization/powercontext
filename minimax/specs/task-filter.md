# Task list specification v2

`list_tasks(tasks, status="all")` returns tasks in their original order.

- `status="all"` returns every task.
- `status="open"` returns only open tasks.
- `status="done"` returns only completed tasks.
- Any other status raises `ValueError` with a message that names the unsupported value.

The v2 status filter supersedes v1. Existing callers that omit `status` remain compatible.
