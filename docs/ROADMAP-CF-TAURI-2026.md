# WeftMark architecture review and delivery roadmap

Status: 2026-11-02; repository-grounded review, roadmap proposal, not a claim of completed deployments.

## Existing foundations

- **Core**: application services for Change Sets, claims, tasks, review decisions, evidence, lifecycle and handoffs; domain and port interfaces.
- **Local adapters**: Git and forge providers, JSONL/SQLite ledger, Nostoi verification, command evidence.
- **Existing surfaces**: Python CLI, Textual TUI, loopback-only HTTP read and explicitly capability-gated write endpoints, stdio MCP, dependency-free web/review board.
- **Portable artifacts**: exported bundles, kanban projections, CAD/Nostoi evidence projection.
- **Edge direction already specified**: docs/superpowers/specs/2026-09-15-artifact-inspector-edge-design.md recommends one-way digest-addressed snapshots and no writes back to the live ledger.

## Decisions and ownership

| Concern | Authoritative owner | Other surfaces |
| --- | --- | --- |
| Git head, workspace, local command execution | trusted local WeftMark process | inspect status only; no Cloudflare command execution |
| Change Set claims/review decisions | application services + ledger | TUI/CLI/HTTP invoke capability-checked commands |
| Audit integrity | Nostoi plus independently retained checkpoint | Worker displays externally published proofs, never blesses them |
| Immutable exports | local exporter, verified immutable object store | Cloudflare Worker reads snapshots by digest |
| Agent tools/MCP | local MCP service with explicit policy | Tauri may connect through local IPC; Worker never proxy arbitrary MCP |
| Human identity and permission | authenticated local/edge identity boundary | never equate caller-supplied actor string with identity |
| Artifact UI | pure renderer over versioned projections | shared web renderer in browser and Tauri; no second state machine |

## P0: release and provenance (started)

1. Keep all builds on GitHub-hosted Actions; build at immutable tagged SHA with locked dependencies.
2. Require release tag to be reachable from main and reject duplicate release publication.
3. Run complete test/build, Debian/Ubuntu clean installs and packaged terminal smoke on the **release tag** (not only main CI).
4. Require GitHub artifact provenance attestations and checksum manifest; verify attestations externally using `gh attestation verify` and enforce tag immutability at the repository ruleset level.
5. Prefer draft release followed by checks before promotion to publicly visible; avoid pushing generated artifacts back to main.

## P0: security boundary before remote exposure

1. Cloudflare Worker v0 is **read-only**: GET/HEAD only, no write routes, no arbitrary URLs, no fetch to loopback/internal network, no arbitrary filesystem paths, no GitHub write token and no direct ledger connection.
2. Exporter produces secret-stripped, bounded JSON snapshots; validate schema, size, SHA-256, version and expiry. Upload immutable objects by digest to private R2; enforce tenant-specific object prefixes rather than trusting a path field from incoming JSON.
3. Cloudflare Access (or verified signed identity JWT) must precede private reads. Public transparency artifacts require an explicit publication policy, not default public access.
4. Reverify trusted ledger head or supplied independent checkpoint; show stale/unverified as unavailable, not green readiness.
5. Tauri v0 must isolate untrusted rendered content from OS capabilities; narrow command allowlist, no shell plugin, no universal filesystem plugin; use trusted local core process with explicit IPC permissions.

## P1: reconciliation and useful refactoring

- Unify **CLI/TUI/HTTP/MCP** around application-layer command/query handlers and typed DTOs. Keep transport-specific identity and authorization at each adapter boundary.
- Consolidate `JsonlLedger` / `SqliteLedger` / Nostoi integrations behind a ledger port plus checkpoint policy; do not collapse different audit semantics into a Boolean.
- Replace parallel CAD/evidence readiness representations with one **inert evidence provenance projection**, one explicit eligibility evaluation, and one human review decision. Never infer approval from a passed test.
- Establish a common versioned artifact/viewer schema for web/review, edge Worker and Tauri. Keep original artifacts immutable, hydrate progressively, and render only safe text.
- Reconcile `frog_*` transition adapters with native task/claim services using an anti-corruption layer; retain migration tooling but avoid a second mutable task state machine.
- Introduce a packaging manifest for CLI, TUI, MCP, architectures and package tests; use a reusable workflow for CI and releases.
- Centralize request validation, size quotas, token handling and redaction across HTTP/MCP/edge; no implicit CORS or ambient auth forwarding.

## P2: capabilities and rollout

**Cloudflare Worker**: static viewer on Pages or Worker; private R2 snapshot GET and schema/digest check; Access auth; immutable URLs and refresh manifest; read-only release-evidence inspection. Not a remotely writable WeftMark server.

**Tauri**: local-first desktop shell reusing web/review components and a dedicated low-privilege WeftMark sidecar. On Linux, sandbox with OS permissions (user namespaces/bubblewrap or systemd unit), distinct data roots, and narrow IPC; do not rely on `chroot` alone. Windows/macOS equivalents need their own threat-model tests. Never expose unrestricted Tauri shell, HTTP, filesystem or process operations to downloaded content.

**Mobile/PWA**: review-only offline snapshot cache, accessibility and localization; explicit refresh and stale-state disclosure. No hidden mutable sync.

**Rebekah/v-baz**: optional integration that composes WeftMark core and local authenticated gateway; offline operation remains first-class. Ephor remains optional governance integration, not a prerequisite for review.

## Acceptance gates

- P0 release: tag-specific CI green, four Linux CLI/TUI release assets and Python/docs archives, checksum verified, attestation query verified, no overwritten release.
- P0 edge: zero write API, no arbitrary outbound network, Access auth, fixed content addressing and tamper/expiry tests; never serves credentials.
- P0 Tauri: capabilities audited, OS-sandboxed sidecar, no renderer direct access to credentials/ledger, explicit IPC negative tests.
- P1 core: CLI/TUI/MCP/HTTP behavior parity, end-to-end Nostoi head anchoring, consistent stale/failed/unavailable distinctions.

## Scope and risks

These proposals do **not** assert that Cloudflare or Tauri deployment already exists. Source lineage and release signing are independent checks. Container isolation is not a security boundary equivalent to a VM for malicious native workloads. Avoid widening write access merely to make remote control convenient.
