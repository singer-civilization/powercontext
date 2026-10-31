# EdgeVault · 端侧向量检索

浏览器里算向量，seekdb 里做混合检索。一个不依赖云端推理的迷你知识库。

## 它解决什么

- 想把资料做成可检索的个人知识库，又不想把原文和向量上传到别人的服务；
- 端侧模型要么体积大、要么慢，需要知道量化到底损失了多少、值不值。

整个流程：**导入语料 → 浏览器本地向量化 → 写入 seekdb → 向量 + 全文混合检索**。

## 技术实现

| 环节 | 做法 |
| --- | --- |
| 端侧推理 | `@xenova/transformers`（Transformers.js v2.17.2）+ ONNX Runtime Web，模型 `Xenova/all-MiniLM-L6-v2`（384 维），**全部在浏览器里跑**，无任何云端推理请求 |
| 推理后端 | 优先 `WebGPU`（`navigator.gpu.requestAdapter()` 成功即用），否则自动降级 `WASM`，界面右上角标签实时显示当前后端 |
| 存储 | `seekdb`（1.4.0，嵌入式模式，数据落在 `./.seekdb`），BFF 用 `SeekdbClient` + `Schema` 建 `vectorIndex(hnsw, cosine)` 与 `fulltextIndex(ik)` |
| 混合检索 | `collection.hybridSearch({ query: { whereDocument: { $contains } }, knn: { queryEmbeddings } })` —— **一次调用在 seekdb 内完成全文条件过滤 + 向量近邻**，应用层不拼接两路结果 |
| 向量化位置 | 向量由浏览器算出后 POST 给 BFF，BFF 只做 `add` / `hybridSearch`，**不在服务端算向量** |

> 中文提问很少会整句出现在正文里，所以检索按「整句 → 前 4 字 → 纯向量」逐级降级；每一步仍在 seekdb 内部完成，只是全文条件放宽。

## 实测数据（2026-10-31，Chrome + Apple Silicon）

INT8 量化模型：

| 指标 | 值 |
| --- | --- |
| 推理后端 | WebGPU |
| 模型体积 | 23 MB（INT8） vs 90 MB（FP32），压缩比 **3.9×** |
| 模型加载 | 159 ms（浏览器已缓存） |
| 向量化 | 19 ms/次，批量 15 块共 512 ms |
| seekdb 写入 | 92 ms |
| 混合检索 | 27 ms |

量化前后对比（同一批 query 重新加载 FP32 计算）：

| 指标 | INT8 | FP32 |
| --- | --- | --- |
| 单条推理 | 7 ms | 5 ms |
| 重新加载 | — | 27.2 s（含 90MB 下载） |
| 向量余弦相似度 | **0.9840**（对 INT8 与 FP32 向量逐条取余弦后平均） | |

结论：INT8 把体积压到 1/4，语义几乎不变（0.984），代价是单条推理在 WebGPU 上略慢一点点（7ms vs 5ms，这个量级更可能是测量抖动）；真正的收益是首次加载时间。

## 运行

```bash
cd stepfun
npm install --legacy-peer-deps      # seekdb 的 peer 依赖声明有点旧，需要这个参数
node server.mjs                     # http://127.0.0.1:8787
```

打开页面，依次点「加载模型」→「导入示例笔记」→ 输入问题点「检索」，再点「量化前后对比」看 INT8/FP32 差异。

首次加载模型会从 Hugging Face 下载约 23MB（INT8），之后走浏览器缓存，可离线使用。

## 目录

```
stepfun/
├── server.mjs          # BFF：seekdb 嵌入式存储 + hybridSearch，不参与向量化
├── public/
│   ├── index.html      # 单页界面
│   ├── app.js          # 端侧推理、切块、入库、检索、量化对比
│   └── vendor/         # Transformers.js 与 ORT wasm（本地托管，不依赖 CDN）
├── samples/            # 三份示例笔记
└── README.md
```

## 与赛题要求的对应

- 导入语料 → 本地向量化 → 写入 seekdb → 混合检索：`app.js` 的 `doIngest()` + `search()`
- 端侧运行、禁云端推理：模型与推理都在页面里，BFF 只收向量
- 量化对比：`compare()` 会重新加载 FP32 模型，输出体积、加载、推理与余弦相似度
- WebGPU 优先 / WASM 降级：`detectBackend()`，界面标签显示当前后端
