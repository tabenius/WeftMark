import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import worker from "../src/index.js";

const digestOf = (bytes) => createHash("sha256").update(bytes).digest("hex");
const artifact = Buffer.from(JSON.stringify({ schema: "weftmark.kanban-projection.v0", cards: [], counts: {} }));
const digest = digestOf(artifact);
const env = (bytes = artifact) => ({
  PRIVATE_ACCESS_ENFORCED: "true",
  ARTIFACTS: {
    get: async (key) => key === `sha256/${digest}.json`
      ? { arrayBuffer: async () => Uint8Array.from(bytes).buffer } : null,
  },
});
const url = `https://example.invalid/v1/artifacts/${digest}`;

test("refuses unsafe methods and missing Access configuration", async () => {
  assert.equal((await worker.fetch(new Request(url, { method: "POST" }), env())).status, 405);
  assert.equal((await worker.fetch(new Request(url), { ...env(), PRIVATE_ACCESS_ENFORCED: undefined })).status, 503);
});
test("serves only a verified R2 object at an exact digest route", async () => {
  const ok = await worker.fetch(new Request(url), env());
  assert.equal(ok.status, 200);
  assert.equal(await ok.text(), artifact.toString());
  assert.equal((await worker.fetch(new Request(url, { method: "HEAD" }), env())).status, 200);
  assert.equal((await worker.fetch(new Request(url + "?url=http://127.0.0.1"), env())).status, 404);
});
test("refuses substituted and oversized R2 data", async () => {
  assert.equal((await worker.fetch(new Request(url), env(Buffer.from("different")))).status, 409);
  assert.equal((await worker.fetch(new Request(url), env(Buffer.alloc(2_000_001)))).status, 413);
});
