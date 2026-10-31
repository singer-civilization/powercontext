# SDD Context Lab（MiniMax 赛题）

这是一个可复现的 SDD 工程上下文模块。它把当前规格、Python 代码和测试写入嵌入式 seekdb，检索出带文件、行号、符号、版本及校验和的上下文；任务进度由 PowerContext 持久化；最后把两者组合成的上下文交给 MiniMax 生成统一 diff。

仓库中不包含 API Key。实时调用使用环境变量，生成记录只保存模型名、请求 ID、token 用量和上下文哈希，不保存密钥。

## 架构

```text
spec / code / tests ──ingest──> embedded seekdb ──retrieve──┐
                                                            ├─> cited SDD context ─> MiniMax ─> git diff
task progress/results ──remember/search──> PowerContext ────┘
```

入口是 `sdd-context` CLI：

- `ingest`：导入或刷新规格、代码和测试。
- `context`：按任务检索并输出带引用的工程上下文。
- `state-save` / `state-load`：通过 PowerContext 保存和恢复进度。
- `generate`：把实际检索上下文交给 MiniMax，保存 patch 和调用元数据。

## 1. 干净环境安装

需要 Python 3.11+，macOS 或 Linux（嵌入式 seekdb 暂不支持 Windows）。

```bash
cd minimax
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[dev]'
```

依赖和 API 用法来自 seekdb 官方的 [Python binding](https://github.com/oceanbase/seekdb-bindings/tree/main/python) 与 [seekdb README](https://github.com/oceanbase/seekdb/blob/master/README.md)。

## 2. 启动 PowerContext

另开一个终端，按 PowerContext 官方文档安装并启动本地服务：

```bash
uv tool install --force 'powercontext[cli,server]==1.2.0'
powercontext server run
```

本地默认地址为 `http://127.0.0.1:8000`，服务会创建一个默认 Scope。取得 Scope ID：

```bash
curl -s http://127.0.0.1:8000/v1/scopes
export POWERCONTEXT_SCOPE_ID='<返回结果中的 scope_id>'
export POWERCONTEXT_URL='http://127.0.0.1:8000'
```

如服务启用了认证，再设置 `POWERCONTEXT_API_KEY`。PowerContext 的安装、持久数据库和重启行为见其官方 [Install and run](https://github.com/oceanbase/powercontext/blob/master/docs/en/docs/get-started/install-and-run.md)。

## 3. 导入并查看上下文

```bash
sdd-context --root . ingest
sdd-context --root . state-save --file evidence/task-state.json
sdd-context --root . context \
  'Implement the v2 task status filter and make done-filter tests pass' \
  --task-id task-filter \
  --output evidence/live/context.md
```

`context.md` 会明确列出：适用规格版本、代码文件和符号、目标测试、已完成事项、待办事项和验证结果。每条数据库引用形如 `path:line#checksum`，不是固定模板输出。

模拟进程重启后，用一个新命令恢复同一状态：

```bash
sdd-context state-load task-filter
```

## 4. MiniMax 实时生成

从 MiniMax 开放平台取得 Key，在当前 shell 中设置（不要写入文件或提交）：

```bash
export MINIMAX_API_KEY='<your-key>'
export MINIMAX_MODEL='MiniMax-M3.1-Flash'
export MINIMAX_API_URL='https://api.minimaxi.com/v1/chat/completions'

sdd-context --root . generate \
  'Implement the v2 task status filter and make done-filter tests pass' \
  --task-id task-filter \
  --output evidence/live/minimax.patch
```

平台会下架旧模型 id：`MiniMax-M2.7`、`MiniMax-M2.5`、`MiniMax-M2` 现在都返回 `2069 The model ... is no longer available`。换模型前先用 `curl -H "Authorization: Bearer $MINIMAX_API_KEY" https://api.minimaxi.com/v1/models` 查当前可用列表；账户余额为 0 时接口返回 `402 insufficient balance (1008)`。

该命令不会自动修改工作树，便于先审查 `minimax.patch`；`python scripts/verify_live_patch.py` 会把这份 patch 打到 before 快照上跑目标测试。旁边的 `minimax.patch.json` 是可提交的最小调用证据；它记录 provider/model/request ID/usage/context SHA-256。默认模型、URL 和请求字段来自 [MiniMax 模型列表](https://platform.minimaxi.com/docs/faq/models) 与 OpenAI 兼容的 `/v1/chat/completions`；模型或网关变化时只需调整两个环境变量。

## 5. 验证

```bash
pytest
ruff check .
python scripts/verify_before_after.py
python scripts/verify_spec_refresh.py
python scripts/verify_live_patch.py
```

`verify_before_after.py` 对 before/after 两个快照运行完全相同的测试文件：before 必须失败，after 必须通过。`verify_spec_refresh.py` 先导入 v1，再用同一路径的 v2 覆盖并重新导入，断言检索结果使用版本 2 和新的 checksum。`verify_live_patch.py` 用 `git apply --recount` 把 MiniMax 真实生成的 patch 打到 before 快照上并跑目标测试。

竞赛现场完整证据应放在忽略提交的 `evidence/live/`，审查后将脱敏的日志复制到提交材料；不得提交 `.env`、API Key 或私有推理过程。
