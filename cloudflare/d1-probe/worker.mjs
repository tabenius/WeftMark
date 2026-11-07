// Diagnostic bridge, not a product API. Bind only a disposable synthetic DB.
import allowedSql from "./allowed-sql.json";
const allowed = new Set(allowedSql);
const headers = { "Cache-Control": "no-store" };
const reply = (body, status = 200) => Response.json(body, { status, headers });

async function authenticated(request, env) {
  if (!env.PROBE_TOKEN) return false;
  const encoder = new TextEncoder();
  const [actual, expected] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(request.headers.get("Authorization") || "")),
    crypto.subtle.digest("SHA-256", encoder.encode(`Bearer ${env.PROBE_TOKEN}`)),
  ]);
  const a = new Uint8Array(actual), b = new Uint8Array(expected);
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}

export default {
  async fetch(request, env) {
    if (!await authenticated(request, env)) return reply({ error: "unauthenticated" }, 401);
    const expiry = Date.parse(env.EXPIRES_AT);
    if (!Number.isFinite(expiry) || Date.now() >= expiry) return reply({ error: "probe_expired" }, 410);
    const path = new URL(request.url).pathname;
    if (path === "/identity" && request.method === "GET") {
      return reply({ schema: "weftmark.d1-probe.v1", source_revision: env.SOURCE_REVISION,
        code_digest: env.PROBE_CODE_DIGEST, expires_at: env.EXPIRES_AT,
        routing: "fresh-first-primary-session-per-batch" });
    }
    if (path !== "/batch" || request.method !== "POST") return reply({ error: "not_found" }, 404);
    try {
      // Stream-limit before allocating/parsing an unbounded request body.
      const chunks = []; let size = 0;
      const reader = request.body?.getReader();
      if (!reader) return reply({ error: "invalid_plan" }, 400);
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 1048576) { await reader.cancel(); return reply({ error: "too_large" }, 413); }
        chunks.push(value);
      }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      const plan = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      if (plan.schema !== "weftmark.ledger-sql-plan.v1" || plan.atomic !== true ||
          !Array.isArray(plan.statements) || plan.statements.length < 1 || plan.statements.length > 8) {
        return reply({ error: "invalid_plan" }, 400);
      }
      for (const statement of plan.statements) {
        if (!allowed.has(statement.sql) || !Array.isArray(statement.params) || statement.params.length > 40 ||
            statement.params.some(p => p !== null && typeof p !== "string" && !Number.isSafeInteger(p))) {
          return reply({ error: "statement_refused" }, 400);
        }
      }
      // Every HTTP request starts on the primary. A batch is one SQL transaction.
      const session = env.DB.withSession("first-primary");
      const statements = plan.statements.map(s => session.prepare(s.sql).bind(...s.params));
      const results = await session.batch(statements);
      return reply(results.map(r => ({ success: r.success, results: r.results ?? [] })));
    } catch (_) {
      // Do not publish SQL, parameters, credentials or internal database errors.
      return reply({ error: "batch_failed" }, 409);
    }
  },
};
