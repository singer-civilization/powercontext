# ResearchVault

面向学生和研究者的「笔记/论文速查」工具：把散落的 Markdown/TXT 笔记与论文摘录导入
**嵌入式 seekdb**（[pylibseekdb](https://github.com/oceanbase/seekdb-bindings)），
检索结果自带出处（**文件 + 行号**）。解决两个痛点：

- 复习时找不到上学期的笔记，只记得「大概在某个 .md 里提过」；
- 翻几十篇论文摘录找一句话，肉眼 grep 太慢。

依赖只有 **Python 标准库 + pylibseekdb**，无 Web 框架、无向量模型、无外部 API，
数据全部存在本地 `./.vault/`。

## 安装

要求 Python ≥ 3.11（pylibseekdb 的限制）。

```bash
cd qoder
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## 使用

统一入口 `python -m researchvault ...`（安装后也可直接用 `researchvault` 命令）。
数据目录默认 `./.vault/`，可用环境变量 `RESEARCHVAULT_HOME` 覆盖。

### `add <path...>` — 递归导入 .md/.txt

```bash
$ python -m researchvault add samples
+ samples/数据库索引笔记.md（1 块）
+ samples/机器学习复习提纲.md（1 块）
+ samples/论文摘录_Attention_Is_All_You_Need.md（1 块）
导入 3 个文件，跳过 0 个未变化文件
```

同一文件重复导入按校验和判断，内容没变就跳过，**不会重复计数**：

```bash
$ python -m researchvault add samples
= samples/数据库索引笔记.md（内容未变，跳过）
= samples/机器学习复习提纲.md（内容未变，跳过）
= samples/论文摘录_Attention_Is_All_You_Need.md（内容未变，跳过）
导入 0 个文件，跳过 3 个未变化文件
```

### `search "<query>" [--limit N] [--json]` — 带出处的关键词检索

```bash
$ python -m researchvault search "覆盖索引"
samples/数据库索引笔记.md:13  [5.0]  - 覆盖索引：如果查询列全部包含在二级索引里，就不用回表。`EXPLAIN` 里显示 `Using index`。

$ python -m researchvault search "多头注意力" --limit 3
samples/论文摘录_Attention_Is_All_You_Need.md:16  [5.0]  - 多头注意力让模型同时关注不同位置、不同表示子空间的信息。
```

输出即「`文件:行号 + [分数] + 摘录`」，按分数降序；行号可直接 `sed -n '13p'` 回到原文。
`--json` 适合喂给其他脚本：

```bash
$ python -m researchvault search "过拟合 正则化" --json --limit 2
[
  {
    "file": "samples/机器学习复习提纲.md",
    "line": 7,
    "score": 3.0,
    "excerpt": "- 模型太简单 → 欠拟合（高偏差）；模型太复杂 → 过拟合（高方差）。"
  }
]
```

未知查询返回空结果，不报错：

```bash
$ python -m researchvault search "量子纠缠熵"
没有匹配“量子纠缠熵”的内容。
```

### `stats [--json]` — 库存概览

```bash
$ python -m researchvault stats
文档数: 3
块数:   3
最近导入: 2026-10-31 17:13:40
数据目录: /Users/changmiaowei/Documents/ChatGPT/OB/powercontext-qoder/qoder/.vault
```

## 测试

真实跑嵌入式 seekdb，不做任何 mock（每个用例独享 `RESEARCHVAULT_HOME` 临时目录）：

```bash
$ pytest -q
.....
5 passed in 14.00s
```

覆盖：导入并按行号命中、打分排序、**重复导入不重复计数**、未知查询返回空、CLI 三命令 JSON 输出。

## 设计说明

### 目录与进程模型

```
${RESEARCHVAULT_HOME:-./.vault}/
└── seekdb-instance/     # pylibseekdb.open() 的嵌入式数据目录
```

每条命令 = 打开实例 → 建表（IF NOT EXISTS）→ 执行 → 关闭。单机单进程使用，无服务常驻。

### 表结构

`rv_documents`（每个文件一行，主键为绝对路径的 sha256，保证**同一重复导入只更新不新增**）：

| 列 | 说明 |
|---|---|
| doc_id PK | sha256(绝对路径) |
| abs_path / rel_path | 导入时的绝对路径 / 相对 cwd 的展示路径 |
| checksum | 全文 sha256，`add` 时先比对，未变化直接跳过 |
| chunk_count / char_count | 块数、字符数 |
| version | 每次内容变化 +1 |
| imported_at / updated_at | TIMESTAMP(6)，`stats` 的「最近导入」取 MAX(updated_at) |

`rv_chunks`（检索单元，重新导入某文件时先 `DELETE ... WHERE doc_id` 再插入，行数不翻倍）：

| 列 | 说明 |
|---|---|
| chunk_id PK | sha256(doc_id + 起止行号) |
| doc_id / rel_path | 归属文档 |
| start_line / end_line | 1-based 闭区间，摘录行号 = start_line + 块内偏移 |
| checksum / content | 所在文件版本校验和 / 块原文 |

### 切块规则

按行窗口切块：每块最多 40 行且最多 4000 字符，不重叠；超长行单独成块。
samples 每份笔记不足 40 行，所以各 1 块。
### 打分规则

1. **分词**：ASCII 连续词（字母/数字/下划线）为一个词；连续 CJK 字符为一个词组。
   长度 ≥ 3 的 CJK 词组额外拆出**重叠二元组**参与召回——seekdb 的 `LIKE` 只做子串匹配，
   整串「多头注意力」匹配不到「多头自注意力机制」这种隔字的写法，二元组（多头/注意/意力）可以。
2. **召回**：`content LIKE '%token%'` 的 OR 并集，最多取 500 个候选块。
3. **打分**（Python 侧，大小写不敏感）：
   `score = Σ token 出现次数`（整串与二元组都计入）`+ 4.0`（查询整串原样出现加成）。
   内容为 0 次的候选直接丢弃，所以路径名撞词不会产生假命中。
4. **排序**：`(-score, file, line)` 取前 `--limit`（默认 8）条；
   摘录取块内含词最多的一行（截断 160 字符），行号还原为文件绝对行号。

### 为什么没有参数绑定

pylibseekdb 1.4 的 `Cursor.execute()` 只接受完整 SQL 字符串（无 bound parameters）。
所有动态值统一经过 `store.quote()`：转义 `\` 与 `'`、剔除 NUL 后再拼字面量，
数值一律 `int()` 强转，避免注入。

### 已知限制

- 中文按子串匹配，没有真正的分词/同义词（「回表」搜不到「table lookup 的中文叫」以外的表述）。
- 单字母 ASCII 查询（如 `B`）会按子串命中一批噪声结果，但排在真实命中之后。
- 嵌入式实例同一目录只允许单进程打开，导入时请勿并发运行两条 `add`。

## 目录结构

```
qoder/
├── researchvault/        # 包：cli / store / chunking / config
├── tests/                # pytest（真实 seekdb）
├── samples/              # 3 份示例笔记
├── site/                 # 纯静态单页介绍站（index.html）
└── pyproject.toml
```

## 项目网站

- 线上地址（Qoder Sites，任何人可访问）：https://research-vault-2qh1asea2in.qoder.website
- 站点源码：`qoder/site/index.html`
- 开发与发布记录：`qoder/QODER_LOG.md`

## 代码仓库

- 仓库：https://github.com/singer-civilization/powercontext
- 分支：`qoder-efficiency`
- 目录：`qoder/`
