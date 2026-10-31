# 一页技术说明：SDD Context Lab

## 问题

规格驱动开发容易在三个地方失真：模型读到旧规格、修改没有定位到真实代码/测试、开发中断后丢失已完成工作。本模块把“当前可验证事实”组装成一次模型调用所需的最小工程上下文。

## 数据流与边界

1. `ingest` 读取项目根目录内的 `specs/**/*.md`、`sample_project/src/**/*.py` 和 `sample_project/tests/**/*.py`。路径越界会被拒绝。
2. 每个文档以 SHA-256 标识，写入嵌入式 seekdb 的 `sdd_documents`；同路径内容改变时版本递增并在 `sdd_import_events` 记录 old/new checksum。
3. `context` 用 SQL `LIKE` 召回候选，再以查询词出现次数排序。输出包含相对路径、最佳匹配行、Python 符号、版本和 checksum，调用方可以回查原文。
4. PowerContext 的公开 `/v1/memory/remember` 和 `/v1/memory/search` API 保存/恢复结构化 `TaskState`。新进程只需相同 Scope 和 task ID 即可恢复 completed/todo/validation。
5. `generate` 将数据库检索结果和恢复状态拼成上下文，调用 MiniMax OpenAI-compatible endpoint。输出只接受为待审查的统一 diff，不自动执行。

## 可验证性

- `tests/test_seekdb_store.py` 使用真实嵌入式 seekdb，验证 v1→v2 刷新、版本递增、旧校验和审计以及新内容命中。
- `tests/test_powercontext.py` 用公开 HTTP 合同做进程边界测试：第一个客户端写入，第二个客户端读取。
- `tests/test_minimax_client.py` 验证发送给模型的用户消息确实含 seekdb 引用，而非固定生成。
- `scripts/verify_before_after.py` 对两个代码快照执行同一个测试套件，旧代码失败、新代码通过。
- 实时 MiniMax 证据包含 request ID、model、usage 和 context hash；实时 PowerContext 证据由两条独立 CLI 命令产生。

## 安全与复现

所有凭据只从环境变量读取，`.env`、本地数据库及 `evidence/live/` 均被忽略。默认 MiniMax 行为是“生成但不应用”，避免未审查代码直接修改仓库。项目只依赖 Python、seekdb 官方嵌入式 binding 和 PyMySQL；README 给出从空环境开始的命令。
