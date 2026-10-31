# Qoder 开发记录

本项目的主要开发工作与站点发布均在 **Qoder** 中完成，使用的是 Qoder 官方 CLI（`qoder`），
版本 **1.1.81**，账号 `CMW-888`。

## 环境

| 项 | 值 |
| --- | --- |
| 安装方式 | `curl -fsSL https://qoder.com/install \| bash`（官方脚本，原生二进制） |
| 可执行文件 | `~/.qoder/bin/qodercli/qodercli-1.1.81`，入口 `~/.local/bin/qodercli` |
| 登录方式 | `qoder login` 设备授权（浏览器确认，凭据保存在 `~/.qoder`） |
| 调用方式 | 非交互会话 `qoder -p "<需求>" --permission-mode auto` |

## 会话记录

### 会话 1 —— 实现 ResearchVault（`1f6acb9e-eec1-492c-87d0-851c2cf1e0e5`）

给 Qoder 的需求（原话摘要）：在 `qoder/` 目录下从零实现 ResearchVault，
只用标准库 + `pylibseekdb`；要求 `add` / `search` / `stats` 三个命令、带文件与行号的检索结果、
pytest 覆盖、README、样例数据，并额外产出 `site/` 静态站点；只允许改 `qoder/` 目录。

Qoder 完成的工作：

- 设计并实现包结构：`researchvault/{config,chunking,store,cli}.py`（共约 430 行，含注释）
- 建表与导入去重：同一文件按 SHA-256 校验和判断，重复导入只更新不重复计数
- 关键词打分检索：输出 `文件:行号 [分数] 摘录`，支持 `--limit` / `--json`
- 生成 3 份示例笔记、5 个 pytest 用例，并在 `.venv` 中真实跑 seekdb
- 生成 `sites/index.html`（纯静态单页，内联 CSS，无外部依赖）

该会话在达到 60 轮上限时结束（Qoder 提示 `Reached maximum number of turns (60)`），
代码与测试已完整产出。

### 会话 2 —— 发布站点（`e1ea422f-6c48-4a2c-bef7-a7ba483a5804`）

给 Qoder 的需求：把 `qoder/site` 发布到 Qoder Sites，站点名 `research-vault`，可见范围设为「任何人都可访问」。

Qoder 完成的工作：

- 通过 Sites 技能完成 `prepare_site` → `publish_site`
- 生成站点描述文件 `site/.research-vault.qoder.site`
- 发布结果：`https://research-vault-2qh1asea2in.qoder.website`
- 站点 `access_mode` 已切换为 `public`，匿名访问返回 HTTP 200

## 复核（本地，与 Qoder 会话无关）

```text
$ .venv/bin/python -m pytest -q
5 passed

$ qoder sites list
research-vault | access: public | lifecycle: active

$ curl -s -o /dev/null -w '%{http_code}' https://research-vault-2qh1asea2in.qoder.website/
200
```
