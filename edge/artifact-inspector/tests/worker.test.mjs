import test from "node:test";
import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import worker from "../src/index.js";

const { publicKey, privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const jwk = { ...publicKey.export({ format: "jwk" }), kid: "test-key", alg: "RS256", use: "sig" };
const b64 = value => Buffer.from(JSON.stringify(value)).toString("base64url");
function jwt(changes = {}, key = privateKey) {
  const now = Math.floor(Date.now() / 1000);
  const first = b64({ alg: "RS256", kid: "test-key", typ: "JWT" });
  const second = b64({ iss: "https://team.cloudflareaccess.com", aud: ["weftmark-audience"],
    iat: now, exp: now + 300, ...changes });
  const input = first + "." + second;
  return input + "." + sign("RSA-SHA256", Buffer.from(input), key).toString("base64url");
}
const auth = (token = jwt()) => ({ "CF-Access-Jwt-Assertion": token });
const digestOf = bytes => createHash("sha256").update(bytes).digest("hex");
const artifact = Buffer.from(JSON.stringify({ schema: "weftmark.kanban-projection.v0", cards: [], counts: {} }));
const digest = digestOf(artifact);
const env = (bytes = artifact) => ({
  ACCESS_ISS: "https://team.cloudflareaccess.com",
  ACCESS_AUD: "weftmark-audience",
  ACCESS_JWKS_JSON: JSON.stringify({ keys: [jwk] }),
  ARTIFACTS: {
    get: async key => key === `sha256/${digest}.json`
      ? { size: bytes.length, arrayBuffer: async () => Uint8Array.from(bytes).buffer } : null,
  },
});
const url = `https://example.invalid/v1/artifacts/${digest}`;
const req = (urlValue = url, method = "GET", token = jwt()) =>
  new Request(urlValue, { method, headers: auth(token) });

test("refuses unsafe methods, missing configuration and missing identity", async () => {
  assert.equal((await worker.fetch(req(url, "POST"), env())).status, 405);
  assert.equal((await worker.fetch(req(), { ...env(), ACCESS_JWKS_JSON: undefined })).status, 503);
  assert.equal((await worker.fetch(new Request(url), env())).status, 401);
});
test("refuses forged, expired, wrong-audience and wrong-issuer claims", async () => {
  assert.equal((await worker.fetch(req(url, "GET", jwt({exp: 1})), env())).status, 401);
  assert.equal((await worker.fetch(req(url, "GET", jwt({aud: ["other"]})), env())).status, 401);
  assert.equal((await worker.fetch(req(url, "GET", jwt({iss: "https://wrong.invalid"})), env())).status, 401);
  const other = generateKeyPairSync("rsa", { modulusLength: 2048 });
  assert.equal((await worker.fetch(req(url, "GET", jwt({}, other.privateKey)), env())).status, 401);
});
test("serves only a signed-identity verified R2 object at an exact digest route", async () => {
  const ok = await worker.fetch(req(), env());
  assert.equal(ok.status, 200);
  assert.equal(await ok.text(), artifact.toString());
  assert.equal((await worker.fetch(req(url, "HEAD"), env())).status, 200);
  assert.equal((await worker.fetch(req(url + "?url=http://127.0.0.1"), env())).status, 404);
});
test("refuses substituted and oversized R2 data", async () => {
  assert.equal((await worker.fetch(req(), env(Buffer.from("different")))).status, 409);
  assert.equal((await worker.fetch(req(), env(Buffer.alloc(2_000_001)))).status, 413);
});
