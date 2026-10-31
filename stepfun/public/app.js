import { pipeline, env } from "./vendor/transformers.min.js";

env.allowLocalModels = false;
env.backends.onnx.wasm.wasmPaths = "./vendor/";

const MODEL = "Xenova/all-MiniLM-L6-v2";
const SAMPLES = ["数据库索引笔记.md", "机器学习复习提纲.md", "论文摘录_Attention_Is_All_You_Need.md"];
const $ = (id) => document.getElementById(id);

let useWebGPU = false;
let extractor = null;
let variant = null;

async function detectBackend() {
  if (navigator.gpu) {
    try {
      const adapter = await navigator.gpu.requestAdapter();
      if (adapter) { useWebGPU = true; }
    } catch { /* 忽略，降级 WASM */ }
  }
  $("backend").textContent = `后端：${useWebGPU ? "WebGPU" : "WASM（WebGPU 不可用，已降级）"}`;
  $("backend").className = useWebGPU ? "tag" : "tag warn";
  $("mBackend").textContent = useWebGPU ? "WebGPU" : "WASM";
}

// Xenova/all-MiniLM-L6-v2 官方体积：FP32 约 90MB，INT8 量化约 23MB
function modelSizeMB(quantized) {
  return quantized ? "23 MB（INT8）" : "90 MB（FP32）";
}

async function loadModel(quantized) {
  const t0 = performance.now();
  extractor = await pipeline("feature-extraction", MODEL, {
    quantized,
    device: useWebGPU ? "webgpu" : "wasm",
  });
  const ms = Math.round(performance.now() - t0);
  variant = quantized ? "INT8 量化" : "FP32 原始";
  $("mLoad").textContent = ms + " ms";
  $("mSize").textContent = modelSizeMB(quantized);
  $("modelTag").textContent = `模型：${variant}`;
  $("modelTag").className = quantized ? "tag" : "tag warn";
}

async function embed(texts) {
  const t0 = performance.now();
  const tensor = await extractor(texts, { pooling: "mean", normalize: true });
  const ms = performance.now() - t0;
  const dim = tensor.dims[tensor.dims.length - 1];
  const data = Array.from(tensor.data);
  const vectors = [];
  for (let i = 0; i < texts.length; i++) vectors.push(data.slice(i * dim, (i + 1) * dim));
  return { vectors, ms, dim };
}

function chunk(text, path) {
  const out = [];
  let line = 1;
  for (const block of text.split(/\n{2,}/)) {
    const lines = block.split("\n");
    const heading = lines.filter((l) => /^#{1,6}\s/.test(l)).map((l) => l.replace(/^#{1,6}\s*/, "")).join(" ");
    const body = lines.filter((l) => !/^#{1,6}\s/.test(l)).join("\n").trim();
    const content = (heading ? heading + "：" : "") + body;
    if (content.trim()) out.push({ id: `${path}:${line}`, path, line, text: content.trim().slice(0, 400) });
    line += lines.length + 1;
  }
  return out;
}

async function ingest() {
  $("ingestBtn").disabled = true;
  $("hint").textContent = "正在读取示例笔记并本地向量化…";
  try {
    await doIngest();
  } catch (e) {
    $("hint").textContent = "入库失败：" + (e && e.message ? e.message : String(e));
    $("ingestBtn").disabled = false;
  }
}

async function doIngest() {
  const chunks = [];
  for (const name of SAMPLES) {
    const res = await fetch(`/samples/${encodeURIComponent(name)}`);
    chunks.push(...chunk(await res.text(), name));
  }
  const vectors = [];
  let ms = 0;
  for (let i = 0; i < chunks.length; i += 6) {
    const batch = chunks.slice(i, i + 6);
    const r = await embed(batch.map((c) => c.text));
    vectors.push(...r.vectors);
    ms += r.ms;
  }
  $("mEmbed").textContent = `${(ms / chunks.length).toFixed(0)} ms/块`;
  const docs = chunks.map((c, i) => ({ ...c, vector: vectors[i] }));
  const res = await fetch("/api/ingest", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ docs }),
  });
  const data = await res.json();
  if (!res.ok) { $("hint").textContent = "入库失败：" + data.error; return; }
  $("mChunks").textContent = data.stored;
  $("searchBtn").disabled = false;
  $("compareBtn").disabled = false;
  $("hint").textContent = `已入库 ${data.stored} 块（浏览器端向量化 ${ms.toFixed(0)} ms，seekdb 写入 ${data.ms} ms）`;
}

async function search() {
  const query = $("query").value.trim();
  if (!query) return;
  const { vectors, ms } = await embed([query]);
  $("mEmbed").textContent = `${ms.toFixed(0)} ms/次`;
  const res = await fetch("/api/search", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, vector: vectors[0], topK: 5 }),
  });
  const data = await res.json();
  if (!res.ok) { $("results").innerHTML = `<div class="muted">检索失败：${data.error}</div>`; return; }
  $("mSearch").textContent = data.ms + " ms";
  $("results").innerHTML = data.hits.length
    ? data.hits.map((h) => `<div class="hit"><div class="src">${h.path}:${h.line}</div><div class="txt">${h.text}</div><div class="dist">distance ${Number(h.distance).toFixed(4)}</div></div>`).join("")
    : `<div class="muted">seekdb 没有返回命中（全文条件可能过严，换个关键词试试）</div>`;
}

function cosine(a, b) {
  let dot = 0;
  for (let i = 0; i < a.length; i++) dot += a[i] * b[i];
  return dot;
}

async function compare() {
  $("compareBtn").disabled = true;
  $("hint").textContent = "加载 FP32 原始模型做对比（体积更大、速度更慢）…";
  const int8 = extractor;
  const t0 = performance.now();
  const fp32 = await pipeline("feature-extraction", MODEL, { quantized: false, device: useWebGPU ? "webgpu" : "wasm" });
  const fp32Load = Math.round(performance.now() - t0);

  const probes = ["覆盖索引为什么快", "注意力机制怎么算", "过拟合怎么办"];
  const t1 = performance.now();
  const a = await int8(probes, { pooling: "mean", normalize: true });
  const int8Ms = performance.now() - t1;
  const t2 = performance.now();
  const b = await fp32(probes, { pooling: "mean", normalize: true });
  const fp32Ms = performance.now() - t2;

  const dim = a.dims[a.dims.length - 1];
  const av = Array.from(a.data), bv = Array.from(b.data);
  let sim = 0;
  for (let i = 0; i < probes.length; i++) {
    sim += cosine(av.slice(i * dim, (i + 1) * dim), bv.slice(i * dim, (i + 1) * dim));
  }
  sim /= probes.length;

  $("hint").textContent =
    `量化对比：INT8 vs FP32，向量余弦相似度 ${sim.toFixed(4)}；` +
    `推理 ${(int8Ms / probes.length).toFixed(0)} ms vs ${(fp32Ms / probes.length).toFixed(0)} ms；` +
    `FP32 重新加载 ${fp32Load} ms。模型体积 ${$("mSize").textContent}（INT8）。`;
  $("compareBtn").disabled = false;
  extractor = int8;
}

await detectBackend();
$("initBtn").addEventListener("click", async () => {
  $("initBtn").disabled = true;
  $("hint").textContent = "正在下载并初始化模型（首次约 23MB，之后走浏览器缓存）…";
  await loadModel($("quantToggle").checked);
  $("ingestBtn").disabled = false;
  $("hint").textContent = "模型就绪，导入示例笔记。";
});
$("ingestBtn").addEventListener("click", ingest);
$("searchBtn").addEventListener("click", search);
$("compareBtn").addEventListener("click", compare);
$("query").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
