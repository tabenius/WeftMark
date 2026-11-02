# Optional Cloudflare artifact inspector (experimental)

This Worker is a **read-only R2 artifact viewer endpoint**, not a WeftMark server or release approval authority.

## Trust and deployment

1. Set Cloudflare Access to protect the **entire Worker hostname and route**, including all API paths, before assigning a custom domain. The Worker now verifies the `CF-Access-Jwt-Assertion` RS256 signature using operator-provisioned `ACCESS_JWKS_JSON`, and checks `ACCESS_ISS`, `ACCESS_AUD` and expiry. Configure these bindings from the exact Cloudflare Access application and rotate JWKS with Access signing keys. Keep Cloudflare Access route enforcement enabled as defense in depth.
2. Bind a **private R2 bucket** as `ARTIFACTS`. The Worker fetches only `sha256/<64-lowercase-hex>.json`. Neither callers nor JSON artifacts can specify a remote URL.
3. Publish exports from a trusted system that redacts secrets/paths, validates schema and enforces strict quotas, then stores each immutable object keyed by its digest.
4. The Worker computes SHA-256 and refuses mismatches. It has no write methods, local-WeftMark connection, token forwarding, dynamic origins or CORS headers.
5. Pin Cloudflare account and route configuration in audited deployment infrastructure; define a separate account/Access policy for public transparency artifacts.

## Current limits

The prototype is intentionally **not automatically deployed**. It verifies JWT signatures but does not yet enforce tenant-specific authorization, provide automatic JWKS rotation, or resolve secret-stripping and schema migration. Even with upstream Access configured, Worker-provided digest integrity does not establish who produced the object. Do not place a private workspace snapshot in a publicly readable R2 bucket.

Run local tests with `cd edge/artifact-inspector && npm test`. CI tests prohibited methods, absent access configuration, path/query injection, mismatched content digests, oversized data and HEAD/GET behavior. The next step is a verified Access JWT / tenant-scope adapter and deployment policy tests, not write capabilities.

## Authentication configuration

Bind `ACCESS_ISS` to the expected Access issuer URL, `ACCESS_AUD` to the Access application's exact audience, and `ACCESS_JWKS_JSON` to a trusted JWKS JSON document retrieved by the operator from that Access tenant. This proof is a read authentication boundary only: if multiple tenants share a Worker, add a separate trusted tenant authorization mapping before enabling their private artifacts. Do not take these settings from request parameters or headers. CI verifies valid signatures and refuses forged, expired and wrong-audience JWTs.
