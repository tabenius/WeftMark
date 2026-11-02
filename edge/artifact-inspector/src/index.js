/**
 * Optional WeftMark edge artifact inspector: read-only R2 digest addressing.
 *
 * No proxy to local WeftMark, no writes, no credential forwarding.
 * Place behind Cloudflare Access; configure access at the route/zone level.
 * This is not an identity-verification service.
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const headers = {
      "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff",
      "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
      "Referrer-Policy": "no-referrer",
    };
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { ...headers, Allow: "GET, HEAD" } });
    }
    // Access must protect the Worker route externally. A client-provided
    // CF-Access-Jwt-Assertion is not sufficient proof of verification.
    if (env.PRIVATE_ACCESS_ENFORCED !== "true") {
      return new Response("Private access not configured", { status: 503, headers });
    }
    const match = /^\/v1\/artifacts\/([0-9a-f]{64})$/.exec(url.pathname);
    if (!match || url.search || url.hash) {
      return new Response("Not found", { status: 404, headers });
    }
    if (!env.ARTIFACTS) {
      return new Response("Storage unavailable", { status: 503, headers });
    }
    const digest = match[1];
    const object = await env.ARTIFACTS.get(`sha256/${digest}.json`);
    if (!object) {
      return new Response("Not found", { status: 404, headers });
    }
    const bytes = await object.arrayBuffer();
    if (bytes.byteLength > 2_000_000) {
      return new Response("Artifact exceeds limit", { status: 413, headers });
    }
    const actual = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)))
      .map((b) => b.toString(16).padStart(2, "0")).join("");
    if (actual !== digest) {
      return new Response("Artifact digest mismatch", { status: 409, headers });
    }
    try {
      const doc = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      if (!doc || typeof doc.schema !== "string" || !doc.schema.startsWith("weftmark.")) {
        return new Response("Unsupported artifact schema", { status: 415, headers });
      }
    } catch {
      return new Response("Invalid artifact", { status: 422, headers });
    }
    return new Response(request.method === "HEAD" ? null : bytes, {
      status: 200, headers: { ...headers, "Content-Type": "application/json; charset=utf-8" },
    });
  },
};
