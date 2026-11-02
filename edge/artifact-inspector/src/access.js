/** Verify Cloudflare Access RS256 JWT with provisioned public JWKs.
 * ACCESS_JWKS_JSON must come from an operator-managed secret/config binding;
 * the caller must never supply keys, issuer, or audience.
 */
function decodePart(part) {
  if (!/^[A-Za-z0-9_-]+$/.test(part)) throw Error("Invalid JWT segment");
  const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(b64), c => c.charCodeAt(0));
}
function parseJSON(part) {
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(decodePart(part)));
}
export async function verifyAccess(request, env) {
  if (!env.ACCESS_ISS || !env.ACCESS_AUD || !env.ACCESS_JWKS_JSON) return false;
  const assertion = request.headers.get("CF-Access-Jwt-Assertion");
  if (!assertion || assertion.length > 8192) return false;
  const parts = assertion.split(".");
  if (parts.length !== 3) return false;
  try {
    const header = parseJSON(parts[0]);
    const payload = parseJSON(parts[1]);
    if (header.alg !== "RS256" || typeof header.kid !== "string" || !header.kid) return false;
    if (payload.iss !== env.ACCESS_ISS || !Array.isArray(payload.aud) || !payload.aud.includes(env.ACCESS_AUD)) return false;
    const now = Math.floor(Date.now() / 1000);
    if (!Number.isSafeInteger(payload.exp) || payload.exp <= now ||
        !Number.isSafeInteger(payload.iat) || payload.iat > now + 60 ||
        (payload.nbf !== undefined && (!Number.isSafeInteger(payload.nbf) || payload.nbf > now))) return false;
    const jwks = JSON.parse(env.ACCESS_JWKS_JSON);
    if (!Array.isArray(jwks.keys)) return false;
    const keys = jwks.keys.filter(k => k.kid === header.kid && k.kty === "RSA" &&
      k.alg === "RS256" && k.use === "sig");
    if (keys.length !== 1) return false;
    const key = await crypto.subtle.importKey("jwk", keys[0],
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    return await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key,
      decodePart(parts[2]), new TextEncoder().encode(parts[0] + "." + parts[1]));
  } catch {
    return false;
  }
}
