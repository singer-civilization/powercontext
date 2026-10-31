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

2026-10-31 15:30（Asia/Shanghai）在本机实测，结论：**链路通了，但账户余额为 0，真实生成还未发生**。`evidence/live/` 保持为空，不伪造 provider 记录。

- **TLS**：旧实现用 `urlopen` 的默认上下文，首次调用报 `CERTIFICATE_VERIFY_FAILED`；改为 `ssl.create_default_context(cafile=certifi.where())` 并把 `certifi>=2025.1` 加入依赖后，请求可以正常到达 `api.minimaxi.com`。
- **鉴权**：用平台 Key 请求 `/v1/chat/completions`，返回的是业务错误（模型不可用 / 余额不足）而不是 401，说明 Key 本身有效。
- **模型**：`GET /v1/models` 当前返回 `MiniMax-M3.1-Flash`、`MiniMax-M3`、`MiniMax-M2.7`、`MiniMax-M2.7-highspeed`、`MiniMax-M2.5`、`MiniMax-M2.1`、`MiniMax-M2`。实际调用 `MiniMax-M2.7`、`MiniMax-M2.5`、`MiniMax-M2` 都返回 `2069 The model ... is no longer available`（列表里还在，但已不可调用），默认模型因此改为 `MiniMax-M3.1-Flash`。
- **余额**：控制台显示可用额度 ¥0.00（现金余额、代金券、授信额度均为 0.00，无充值记录）。`MiniMax-M3.1-Flash` 与 `MiniMax-M3`、`MiniMax-Text-01` 均返回 `402 insufficient balance (1008)`。
- **恢复方式**：充值后按 README 第 4 节执行 `sdd-context --root . generate ... --output evidence/live/minimax.patch`，会同时产出 `minimax.patch.json`（provider / model / request ID / usage / context SHA-256，不含 Key）；再用 `python scripts/verify_live_patch.py` 把这份 patch 打到 before 快照上跑目标测试，通过即闭环。

`scripts/verify_live_patch.py` 的用例已用 before→after 快照的等价 diff 验证过：patch 能干净应用，目标测试 3 passed，脚本退出码 0。
