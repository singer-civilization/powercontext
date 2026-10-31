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

2026-10-31 15:5x（Asia/Shanghai）本机真实调用成功，记录来自 `evidence/live/minimax.patch.json`：

```json
{
  "provider": "MiniMax",
  "model": "MiniMax-M3.1-Flash",
  "request_id": "0734d11aac4a1d43c58d46a5f1c726da",
  "usage": {
    "total_tokens": 1540,
    "total_characters": 0,
    "prompt_tokens": 900,
    "completion_tokens": 640,
    "completion_tokens_details": {
      "reasoning_tokens": 443
    },
    "prompt_tokens_details": {
      "cached_tokens": 128
    }
  },
  "context_sha256": "88094493927b95e02837441bbc129cd60c60cf359879c9a873cec9fd6990644c"
}
```

- **TLS**：旧实现用 `urlopen` 的默认上下文，首次调用报 `CERTIFICATE_VERIFY_FAILED`；改为 `ssl.create_default_context(cafile=certifi.where())` 并把 `certifi>=2025.1` 加入依赖后，请求可以正常到达 `api.minimaxi.com`。
- **模型**：`MiniMax-M2.7`/`M2.5`/`M2` 仍出现在 `GET /v1/models` 里，但实际调用返回 `2069 The model ... is no longer available`；端点切到 `/v1/chat/completions`，默认模型改为 `MiniMax-M3.1-Flash`。
- **余额**：首次实测时账户可用额度为 ¥0.00，所有模型返回 `402 insufficient balance (1008)`；充值到 ¥25.00（现金 10 + 代金券 15）后调用打通。
- **推理模型的两个坑**（都已在代码里处理）：
  1. `MiniMax-M3.1-Flash` 是推理模型，`max_tokens=4096` 时 4096 个 token 全被 `reasoning_content` 吃光，`finish_reason=length` 且没有 `content`。现在默认 `MINIMAX_MAX_TOKENS=16384`，并在 `finish_reason=length` 时明确报错。
  2. 第一次成功返回的内容是一句说明文字而不是 diff（上下文当时只给了一行代码摘录，模型无从下手）。现在 `render_context` 会为每个命中附带完整文件正文，客户端也会在返回值不是 unified diff 时直接报错，不会把散文写进 patch 文件。
- **patch 应用**：模型给出的 hunk 头计数差一（`@@ -10,7 +10,13 @@`，实际 6/12 行），`scripts/verify_live_patch.py` 用 `git apply --recount` 应用，目标测试 3 passed。

复现命令：

```bash
export MINIMAX_API_KEY='<key>'
export MINIMAX_MODEL='MiniMax-M3.1-Flash'
sdd-context --root . generate \
  'Implement the v2 task status filter and make done-filter tests pass' \
  --task-id task-filter --output evidence/live/minimax.patch
python scripts/verify_live_patch.py
```
