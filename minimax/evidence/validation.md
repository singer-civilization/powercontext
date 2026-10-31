# Validation record

Run date: 2026-10-31 (Asia/Shanghai)

## Static and project tests

```text
$ ruff check .
All checks passed!

$ pytest
....                                                                     [100%]
4 passed in 5.62s
```

The four tests cover context rendering, the MiniMax request contract, PowerContext state recovery with a new client, and a real embedded seekdb specification refresh.

## Same target tests before and after

Command: `python scripts/verify_before_after.py`

```text
=== BEFORE (expected failure) ===
F.F                                                                      [100%]
FAILED test_filters_done_tasks - TypeError: list_tasks() got an unexpected keyword argument 'status'
FAILED test_rejects_unknown_status - TypeError: list_tasks() got an unexpected keyword argument 'status'

=== AFTER (expected pass) ===
...                                                                      [100%]
```

The before and after runs use the exact same `sample_project/tests/test_taskboard.py` file.

## seekdb specification refresh

Command: `python scripts/verify_spec_refresh.py`

```json
{
  "before": {
    "version": 1,
    "checksum": "8a8a66e72add10d91080e5915c5dc5a4e060b4313fe0faf018de2fb924ac7af2",
    "top_excerpt": "Each task has an `id`, `title`, and `status`. The supported status values are `open` and `done`."
  },
  "after": {
    "version": 2,
    "checksum": "d5ff3404eae1239de7ac07bc473b0d68705a791f160208b41941e34b0ec7b803",
    "top_excerpt": "The v2 status filter supersedes v1. Existing callers that omit `status` remain compatible."
  },
  "import_events": 2,
  "refreshed_content_used": true
}
```

## PowerContext server restart

1. Started a local PowerContext server with a persistent data directory.
2. Saved `task-filter` with `state-save` and received Memory revision 1.
3. Stopped the server cleanly.
4. Started a new server process against the same data directory.
5. Ran `state-load task-filter`; the original objective, completed items, todo items, validation, and timestamp were recovered.

No inference provider was configured for PowerContext. Explicit Memory remember/search remained database-backed and operational as documented.

## MiniMax live call

The HTTP client and request-context contract are tested locally. A live provider record is intentionally not fabricated: it must be produced with the participant's `MINIMAX_API_KEY` by running the README `generate` command. The resulting `evidence/live/minimax.patch.json` will contain the provider, model, request ID, usage, and context hash without the key.
