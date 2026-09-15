# WeftMark Artifact Inspector — Edge Deployment Design Sketch

> **Status:** design sketch / suggestion. Not a claimed task. Records a
> proposed surface and its interop contract with running ("Actual")
> WeftMark so the approach can be reviewed before any implementation task
> is planned.

**Goal:** A hostable, read-only UI that inspects WeftMark artifacts of
every kind (board projection, Change Sets, evidence, reviews, handoffs,
portable bundles, assurance facts, release evidence, Frog parity), with a
Cloudflare Worker as *one* edge-deployment adapter — never a required
dependency.

**Non-goal:** Any write path back into the ledger. This surface observes;
it never coordinates. That is a hard boundary, not a v1 cut.

## Why this fits WeftMark's existing design

This is not a greenfield idea grafted onto the project — it completes a
boundary the codebase already anticipates:

- `web/review/` is already a dependency-free, read-only client for
  `weftmark.kanban-projection.v0`, and its README already states the
  intended remote-use pattern: *"serve the static files and proxy that
  path to the loopback-only WeftMark HTTP read surface behind an
  authenticated TLS boundary."* This design names and specifies that
  boundary.
- `src/weftmark/http/server.py` already serves the live read model, and
  is deliberately **loopback-only** (`_require_loopback`), Bearer-token
  gated, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  and emits **no CORS headers**. The edge surface must not weaken any of
  these; it sits *outside* them.
- `weftmark bundle export` already produces a portable, digest-anchored,
  secret-and-path-stripped snapshot (`weftmark-portable-bundle-v1`). That
  is the natural artifact to publish to an edge that WeftMark Actual must
  never trust.

Two AGENTS.md rules govern the whole design and are treated as
invariants below: **"Do not build automatic two-way status sync"** and
**"Stale or unknown state fails safe and cannot grant ownership."**

## Artifact kinds the inspector renders

Every kind is already a schema-tagged JSON object, so the UI keys off
`schema` / `format` and renders accordingly:

| Kind | Schema / format tag | Source today | Trust anchor |
|------|--------------------|--------------|--------------|
| Board projection (workspace / per-change / per-task) | `weftmark.kanban-projection.v0` | `GET /v0/kanban[...]` | `generated_at`, live read |
| Portable Change Set bundle | `weftmark-portable-bundle-v1` | `weftmark bundle export` | `digest: sha256:…` over canonical contents |
| Assurance facts rollup | `weftmark.assurance-facts.v0` | `assurance/facts.json` | committed at a SHA |
| Release evidence | `weftmark.release-evidence.v0` | `scripts/build_release_evidence.py` (pending) | embeds `source_sha` + evidence |
| Frog transition projection | `weftmark.frog-transition-projection.v0` | Frog import adapter | snapshot receipt digest |
| Frog parity report | `weftmark.frog-parity-report.v0` | `weftmark frog parity` | capture/import time + staleness |

A bundle already carries `change_set`, `claims`, `evidence`, `reviews`,
and `handoffs` inline, so a single bundle drives most detail views without
extra endpoints.

## Interop with WeftMark Actual — the core decision

Two viable models. The recommendation is to ship **A as the default** and
offer **B only as an operator-local convenience**, because A keeps the
loopback boundary intact and matches WeftMark's own no-two-way-sync rule.

### Model A — Snapshot publish (recommended default)

One-way flow, air-gapped in the direction that matters:

```
WeftMark Actual (loopback, e.g. konsonans)
  └─ weftmark bundle export  /  projection snapshot  /  release-evidence
       → canonical JSON + sha256 digest  (no secrets, no local paths)
         → signed upload → Cloudflare R2 / KV (immutable, keyed by digest)
             → CF Worker  (serves static UI + reads published objects)
                 → viewer (Cloudflare Access / public transparency page)
```

Properties:
- **The edge is a pure read replica of published artifacts.** The Worker
  never opens a connection back to WeftMark Actual. There is no network
  path from the edge into the ledger — the containment is structural, not
  policy.
- **Digest is the trust anchor.** The producer computes it inside the
  boundary; the Worker (and, ideally, the viewer in-page) recomputes and
  compares. A tampered edge object fails verification rather than
  silently misrepresenting state.
- **Staleness is explicit and fails safe.** Every object carries
  `exported_at` / `generated_at`; the UI shows age and refuses to present
  an object past a freshness budget as "current." Matches the parity-report
  staleness discipline already in the codebase.
- **Publication is idempotent.** Objects are keyed by digest (immutable)
  with a small mutable "latest" pointer per stream (board, per Change Set,
  release). Re-publishing the same state is a no-op.

Publisher options (pick per environment, all one-way):
1. `weftmark bundle export` piped to `wrangler r2 object put` / the R2 S3
   API from konsonans (simplest; a cron or post-evidence hook).
2. A tiny `weftmark publish` adapter behind an explicit, auditable outbox
   (the AGENTS.md "explicit, idempotent, auditable outbox with stable
   identity mapping" shape) if publishing becomes routine.

### Model B — Authenticated live proxy (operator-only, optional)

For the single operator's own tablet, a `cloudflared` tunnel or Worker
reverse-proxy fronts the loopback `/v0/kanban` behind Cloudflare Access +
mTLS. This is the "authenticated TLS boundary" `web/review/README.md`
already describes.

Use it only when *live* board state on a trusted device is worth exposing
WeftMark Actual to an always-on tunnel. It is strictly more attack surface
than A (the ledger host is now reachable, if gated), so it is not the
default and never the public path.

### Why not a direct edge→ledger API

Rejected: it would require CORS/public bind on the loopback surface
(violates `_require_loopback` and the no-CORS stance), put the ledger on
the far side of an untrusted edge, and invite exactly the two-way coupling
AGENTS.md forbids.

## Security boundary (containment framing)

The design is deliberately shaped as a one-directional trust gradient —
the ledger is the clean side, the edge is the dirty side, and nothing
crosses back:

- **One-way dataflow.** konsonans → R2 → Worker → viewer. No inbound path
  to the ledger. The edge cannot request, mutate, or even enumerate ledger
  state it was not handed.
- **All artifact content is untrusted data at the edge and in the DOM.**
  Projections and bundles contain externally-influenced strings (branch
  names, commit subjects, task titles, dirty paths). The UI renders them
  as text with full escaping and a strict CSP (`default-src 'none'`;
  no inline script; no third-party origins) — never `innerHTML` of raw
  fields, never anything eval-adjacent. This is the same "treat relayed
  content as data" discipline the repo already applies to forge/CI input.
- **No secrets leave the boundary — verified, not assumed.** Bundle export
  already strips them; the design adds a publish-time assertion that every
  published object is free of `repository_id`, absolute paths, tokens, and
  environment descriptions before upload (fail-closed).
- **Integrity over availability.** A failed digest check or an over-age
  object renders an explicit error state, not stale-as-fresh. Unknown
  schema → refuse to interpret.
- **Edge access control is the edge's job.** Cloudflare Access (Zero
  Trust) for the operator UI; only `weftmark.release-evidence.v0` and an
  explicitly-marked-public projection may be served unauthenticated, since
  release evidence is designed to be publishable.

## Vendor-neutrality

WeftMark's principles require open formats and replaceable adapters and
forbid a proprietary cloud dependency in the core. Therefore:

- The inspector UI and all artifact schemas stay **host-agnostic static
  assets** (the `web/review/` lineage): they run on any static server, a
  laptop, or an air-gapped box with a downloaded bundle file.
- Cloudflare is one **deployment adapter**: Worker (serving + edge cache),
  R2/KV (snapshot store), Access (auth). Each maps to a generic role
  (static host, object store, identity gateway) that another provider or a
  self-hosted stack can fill. Nothing in the artifact contract is
  Cloudflare-specific.

## Suggested task slices (dependency-ordered, small)

1. **Extend the read client to multi-artifact rendering.** Teach the
   `web/review/` client (or a sibling `web/inspector/`) to detect and
   render bundles, assurance facts, and release evidence in addition to
   the board projection. Pure static, no cloud. *Evidence: schema-fixture
   render tests.*
2. **Freshness + digest verification in-page.** Show `exported_at`/age,
   recompute and check `sha256` for bundles, explicit stale/tamper states.
   *Evidence: negative-path tests (over-age, wrong digest, unknown schema).*
3. **Publish path (Model A).** A one-way `weftmark`-side export→object-store
   step with a pre-upload secret/path assertion and digest-keyed naming.
   Vendor-neutral object-store port + a Cloudflare R2 adapter. *Evidence:
   export produces byte-identical digest to `weftmark bundle verify`; the
   secret-scrub assertion has refusal tests.*
4. **CF Worker deployment adapter.** `wrangler` project serving the static
   UI, reading R2, strict CSP + security headers, Cloudflare Access on the
   operator route, public only on the release-evidence route. *Evidence:
   header/CSP smoke test; Access-gated route returns 401/redirect
   unauthenticated.*
5. **(Optional) Model B operator live-proxy** as a separately-documented,
   non-default deployment note — not code in the core.

Each slice is independently shippable and leaves the core static UI usable
with a local file if no cloud is present.

## Open questions for review

- Is a public, unauthenticated `release-evidence` transparency page
  desired for the alpha, or is *everything* behind Access?
- Publish cadence: post-evidence hook, cron, or manual `weftmark publish`?
- Should in-page digest verification be mandatory (block render on
  mismatch) or advisory with a prominent warning?
