// ResearchVault 边缘检索 BFF：浏览器算向量，seekdb 负责存储 + 混合检索
import express from "express";
import { SeekdbClient, SeekdbAdminClient, Schema, VectorIndexConfig, FulltextIndexConfig } from "seekdb";

const PORT = process.env.PORT || 8787;
const DIM = 384;
const DB_PATH = process.env.SEEKDB_PATH || "./.seekdb";

const app = express();
app.use(express.json({ limit: "32mb" }));
app.use(express.static("public"));
app.use("/samples", express.static("samples"));

let client;
let collection;

async function ensureDatabase() {
  const admin = new SeekdbAdminClient({ path: DB_PATH });
  const databases = await admin.listDatabases();
  const names = (databases || []).map((d) => (typeof d === "string" ? d : d.name || d.database));
  if (!names.includes("vault")) await admin.createDatabase("vault");
  await admin.close();
  client = new SeekdbClient({ path: DB_PATH, database: "vault" });
}

async function getCollection() {
  if (collection) return collection;
  if (!client) await ensureDatabase();
  const schema = new Schema({
    vectorIndex: new VectorIndexConfig({ hnsw: { dimension: DIM, distance: "cosine" }, embeddingFunction: null }),
    fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
  });
  try {
    collection = await client.createCollection({ name: "chunks", schema });
  } catch {
    collection = await client.getCollection({ name: "chunks" });
  }
  return collection;
}

app.post("/api/ingest", async (req, res) => {
  try {
    const { docs } = req.body;
    if (!Array.isArray(docs) || !docs.length) return res.status(400).json({ error: "docs 不能为空" });
    const col = await getCollection();
    const started = Date.now();
    try { await col.delete({ ids: docs.map((d) => d.id) }); } catch { /* 首次导入没有旧数据 */ }
    await col.add({
      ids: docs.map((d) => d.id),
      documents: docs.map((d) => d.text),
      embeddings: docs.map((d) => d.vector),
      metadatas: docs.map((d) => ({ path: d.path, line: d.line })),
    });
    res.json({ stored: docs.length, ms: Date.now() - started });
  } catch (e) {
    res.status(500).json({ error: String(e.message || e) });
  }
});

app.post("/api/search", async (req, res) => {
  try {
    const { query, vector, topK = 5 } = req.body;
    if (!Array.isArray(vector) || vector.length !== DIM) return res.status(400).json({ error: "vector 维度不对" });
    const col = await getCollection();
    const started = Date.now();

    // 混合检索由 seekdb 执行：全文条件 + 向量近邻，一次调用完成。
    // 中文问题往往不会整句出现在正文里，所以按“整句 → 前 4 字 → 纯向量”逐级降级，
    // 每一步仍然是 seekdb 内部的 hybridSearch / query，不在应用层拼两路结果。
    const clean = String(query).replace(/[\s？?。，,！!、；;：:]+/g, "");
    const attempts = [
      { mode: "hybrid", keyword: query },
      { mode: "hybrid", keyword: clean.slice(0, 4) },
      { mode: "vector", keyword: null },
    ];
    for (const attempt of attempts) {
      let result;
      if (attempt.mode === "hybrid") {
        result = await col.hybridSearch({
          query: { whereDocument: { $contains: attempt.keyword } },
          knn: { queryEmbeddings: [vector] },
          nResults: Number(topK),
        });
      } else {
        result = await col.query({ queryEmbeddings: [vector], nResults: Number(topK) });
      }
      const ids = (result.ids && result.ids[0]) || [];
      if (!ids.length) continue;
      const docs = (result.documents && result.documents[0]) || [];
      const metas = (result.metadatas && result.metadatas[0]) || [];
      const dists = (result.distances && result.distances[0]) || [];
      return res.json({
        mode: attempt.mode,
        keyword: attempt.keyword,
        ms: Date.now() - started,
        hits: ids.map((id, i) => ({ id, text: docs[i], ...(metas[i] || {}), distance: dists[i] })),
      });
    }
    res.json({ mode: "none", ms: Date.now() - started, hits: [] });
  } catch (e) {
    res.status(500).json({ error: String(e.message || e) });
  }
});

app.get("/api/stats", async (req, res) => {
  try {
    const col = await getCollection();
    const all = await col.get({});
    const rows = (all.ids && all.ids[0]) || [];
    res.json({ chunks: rows.length });
  } catch (e) {
    res.status(500).json({ error: String(e.message || e) });
  }
});

app.listen(PORT, () => console.log(`ResearchVault BFF on http://127.0.0.1:${PORT}`));
