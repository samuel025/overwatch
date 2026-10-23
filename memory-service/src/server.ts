// Overwatch memory service: exposes the H-MEM engine over HTTP so the Python agents can use it.
//
//   GET  /healthz                         -> { ok, memories }
//   POST /remember  { content, source, tags?, confidence?, layer?, missionId? }
//   POST /recall    { query, topN?, threshold? }  -> { memories, context, trace }
//   POST /feedback  { memoryId, kind: helpful | not_helpful | wrong }
//   GET  /memories                        -> all records (no embeddings)
//   POST /forget    { memoryId, reason? }  -> audited delete
//   GET  /ledger?limit=50                 -> latest trust-ledger entries
//
// State lives in HMEM_DIR (state.json + ledger.jsonl). Set MEMORY_TOKEN to require a bearer token.

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { HMEM } from "./hmem/index.ts";
import { snapshot } from "./hmem/memoryStore.ts";
import type { FeedbackKind, MemoryLayer } from "./hmem/index.ts";

const PORT = Number(process.env.PORT ?? 8099);
const HOST = process.env.HOST ?? "127.0.0.1";
const DIR = path.resolve(process.env.HMEM_DIR ?? "data");
const TOKEN = process.env.MEMORY_TOKEN ?? "";

const hmem = new HMEM({ persistDir: DIR, actor: "overwatch" });

class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function readBody(req: http.IncomingMessage): Promise<Record<string, any>> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 1_000_000) reject(new HttpError(413, "body too large"));
    });
    req.on("end", () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch {
        reject(new HttpError(400, "invalid JSON body"));
      }
    });
  });
}

function send(res: http.ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

async function route(req: http.IncomingMessage, url: URL): Promise<unknown> {
  const key = `${req.method} ${url.pathname}`;
  if (key === "GET /healthz") return { ok: true, engine: "h-mem", memories: hmem.store.all().length, dir: DIR };

  if (TOKEN && req.headers.authorization !== `Bearer ${TOKEN}`) throw new HttpError(401, "unauthorized");

  switch (key) {
    case "POST /remember": {
      const b = await readBody(req);
      if (typeof b.content !== "string" || !b.content.trim()) throw new HttpError(400, "content is required");
      const layers: MemoryLayer[] = ["short_term", "working", "long_term"];
      const rec = hmem.add({
        content: b.content,
        source: String(b.source ?? ""), // H-MEM rejects an empty source by construction
        type: "factual",
        layer: layers.includes(b.layer) ? b.layer : "working",
        tags: Array.isArray(b.tags) ? b.tags.map(String) : [],
        confidence: typeof b.confidence === "number" ? b.confidence : undefined,
        missionId: b.missionId,
      });
      return { memory: snapshot(rec) };
    }
    case "POST /recall": {
      const b = await readBody(req);
      if (typeof b.query !== "string" || !b.query.trim()) throw new HttpError(400, "query is required");
      const result = hmem.query(b.query, { topN: b.topN, threshold: b.threshold });
      return {
        memories: result.memories.map((m) => ({ ...snapshot(m.memory), relevance: m.relevance })),
        context: result.context,
        trace: result.trace,
      };
    }
    case "POST /feedback": {
      const b = await readBody(req);
      const kinds: FeedbackKind[] = ["helpful", "not_helpful", "wrong"];
      if (!kinds.includes(b.kind)) throw new HttpError(400, `kind must be one of ${kinds.join(", ")}`);
      if (!hmem.store.get(b.memoryId)) throw new HttpError(404, `unknown memory ${b.memoryId}`);
      hmem.feedback(b.memoryId, b.kind);
      return { memory: snapshot(hmem.store.get(b.memoryId)!) };
    }
    case "GET /memories":
      return { memories: hmem.store.all().map(snapshot) };
    case "POST /forget": {
      const b = await readBody(req);
      if (!hmem.store.get(b.memoryId)) throw new HttpError(404, `unknown memory ${b.memoryId}`);
      hmem.store.delete(b.memoryId, String(b.reason ?? "forgotten via API")); // audited in the ledger
      return { ok: true };
    }
    case "GET /ledger": {
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const file = path.join(DIR, "ledger.jsonl");
      const lines = fs.existsSync(file) ? fs.readFileSync(file, "utf-8").trim().split("\n").filter(Boolean) : [];
      return { entries: lines.slice(-limit).map((l) => JSON.parse(l)) };
    }
  }
  throw new HttpError(404, `no route ${key}`);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);
  try {
    send(res, 200, await route(req, url));
  } catch (err) {
    const status = err instanceof HttpError ? err.status : 400;
    send(res, status, { error: (err as Error).message });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`overwatch memory service (H-MEM) on http://${HOST}:${PORT}, state in ${DIR}`);
});
