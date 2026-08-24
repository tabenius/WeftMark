# Frog transition map

WeftMark should retain Frog's proven coordination semantics without making
`AGENTS.db`, `/data/src`, SQLite row shapes, or Frog command names part of its
domain model. Frog task intent becomes imported plan data. A WeftMark Change
Set remains the separate execution envelope that binds intent to exact Git
lineage, semantic scope, evidence, review, and handoff.

This inventory is the `contract:frog-transition-map-v0` baseline for a
read-only importer. It does not authorize writes to a Frog database or claim
that WeftMark has replaced Frog's workspace discovery, scheduling, federation,
or MCP surfaces.

## Observed baseline

The inventory was taken on 2026-08-14 from the local `ragbaz-frog` checkout at
commit `23698ab52356c5c946590a326194ea56464a75e9`. The inspected sources were:

- `ragbaz-frog/README.md` and generated `docs/COMMANDS.md`;
- `ragbaz-frog/src/ragbaz_frog/migrations/001_initial.sql` through
  `013_event_origin_box.sql`;
- `ragbaz-frog/src/ragbaz_frog/store.py`, especially `task_claim`,
  `task_finish`, `task_next`, `lock_acquire`, and `lock_audit`;
- live output from `frog mcp tools --json`;
- `ragbaz-frog/ragbaz.component.json`.

The component manifest advertises `ops.coordination` and sensitive `ops.mcp`
capabilities, requires an `AGENTS.db` storage service and coordination-write
approval, publishes task/lock change events, and describes a host-level tenant
boundary with reference-only secret access. Those operational declarations are
Frog capabilities, not evidence that equivalent WeftMark capabilities already
exist.

The current database records migration names rather than one numeric schema
version. Migrations 001–013 produce repositories, files, agents, tasks and task
relations, assignments and status history, locks, events, units, detected
targets and runs, artifacts, cross-repository dependencies, mirrored events,
repository keys and aliases, provider identities, hooks, box identity, peers,
and event origin metadata.

## Semantic mapping

- **workspace:** Preserve its named source root and remote routing context as
  adapter configuration. Never make an absolute path domain-global.
- **repo:** Preserve stable logical identity and local aliases. The Git port
  supplies observed repository/worktree facts; paths are not portable identity.
- **unit:** Preserve a nested buildable or runnable location as discovery and
  execution-adapter metadata. It is not a repository or Change Set by
  implication.
- **task:** Preserve goal, why, what, ROI, priority, status, ownership, and
  relations as imported plan intent. One task may originate zero or many
  Change Sets; it does not become a Change Set automatically.
- **task dependency:** Preserve the directed plan prerequisite. It gates task
  eligibility, not Git ancestry.
- **task conflict:** Preserve explicit scheduling incompatibility and its
  reason, distinct from overlapping runtime scopes.
- **task tags and parent:** Preserve optional classification and navigation.
  Parentage must not stand in for dependency edges.
- **workflow status:** Preserve planning state with source provenance. Do not
  infer it from Git state or WeftMark readiness.
- **git status:** Preserve Frog's separate publication state as source metadata
  only. WeftMark derives actual Git lineage through its Git port.
- **assignment:** Preserve declared task responsibility as a plan-level actor
  reference. It does not grant a WeftMark scope lease.
- **lock:** Map leased scope/file exclusion and lifecycle to claims containing
  semantic `ScopeLease` values. Imported locks are observations and cannot
  become active local leases.
- **agent:** Reference the coordination actor from claims, evidence producers,
  reviews, and handoffs. The domain does not require a global agent registry.
- **file record:** Preserve classification, source-of-truth notes, and task
  association as plan metadata and `file:` scope inputs. This is not evidence
  that a file changed.
- **repo target:** Preserve detected command, workdir, risk flags, and artifact
  hints as a candidate evidence definition. Detection is not proof.
- **target run:** Convert command results only after validation against an exact
  Change Set observation and privacy policy.
- **event log:** Import mutation events as source provenance where needed. Do
  not replay generic events as authoritative domain transitions.
- **event mirror:** Portable bundle receipt is the current analogue for
  read-only cross-workspace visibility. Authenticity and incremental sync are
  separate future contracts.
- **repo key/alias:** Preserve logical cross-box identity as adapter input.
  Absolute aliases remain local and are stripped from portable records.
- **box/peer:** Preserve federation and routing identity only as transport
  configuration or import provenance; it cannot imply trust.
- **provider source/external ID:** Preserve the idempotency identity under a
  namespace. Provider round-trip writes are outside the first importer.

## Workflow mapping

Frog deliberately composes useful operations. WeftMark should preserve their
outcomes while keeping proof gates explicit.

- **`frog task create/edit/dependency/conflict/tag`:** Create or update
  source-plan intent and relations. Current WeftMark YAML plans are the source
  form; a runtime plan service is still missing.
- **`frog task next`:** Build a future scheduler over dependency, conflict,
  claim, and ROI read models. It must distinguish eligible intent from a
  Change Set ready for review.
- **`frog task claim`:** Create or select a Change Set, then atomically acquire
  its declared scopes. Frog's default one-active-task rule remains scheduler
  policy, not a domain invariant.
- **`frog lock acquire/renew/release/reap`:** Use claim acquire, renew,
  release, and effective expiry. Scope conflicts remain semantic and
  repository-aware.
- **`frog lock audit/check-file`:** Use WeftMark scope audit plus a future
  pre-edit integration. Uncovered work is distinct from a conflicting claim.
- **`frog repo affected`:** Combine a Git observation with declared dependency
  and target selection. Selection alone is not evidence.
- **`frog repo build/test/check/verify`:** Run command evidence bound to the
  current clean Change Set head, retaining privacy-safe metadata and digests.
- **`frog task finish`:** Require evidence policy, scope audit, review, a
  clean-head handoff, explicit lifecycle transitions, and claim release. No
  single status flip may bypass these gates.
- **`frog status/board/repo status`:** Build read models over plan intent and
  Change Set runtime state. Current `weftmark status` covers only the latter.
- **`frog log/why/blame`:** Add ledger provenance queries. Generic event
  summaries are context, not the source of domain truth.
- **`frog sync pull/list` and federation:** Export, verify, import, and inspect
  portable receipts. Cross-box routing, authenticity, and trust policy remain
  unimplemented.
- **`frog mcp serve/tools`:** Put a transport adapter over the same WeftMark
  application services used by the CLI. MCP must not duplicate business rules.

The live Frog MCP catalog exposes workspace, repo, unit, task, lock, log,
affected-target, and health operations. Its write surface includes task
create/edit/dependency/claim/finish and lock acquire/release. The first
WeftMark MCP milestone should expose only already-implemented application
services; it should not claim Frog parity merely by matching tool names.

## Read-only importer contract

The first adapter reads selected Frog state and produces a deterministic source
snapshot. It does not open a WeftMark ledger, create Change Sets, or mutate the
source database.

### Inputs

- a required source label chosen by the operator, stable for that logical Frog
  workspace;
- an `AGENTS.db` path opened with SQLite read-only mode and `query_only`;
- optional repository and task filters;
- the ordered `schema_migrations` names;
- selected rows from `repos`, `tasks`, `task_dependencies`, `task_conflicts`,
  `task_tags`, `task_assignments`, `agents`, `task_files`, `files`, and `locks`.

Repository targets, target runs, generic events, hooks, peers, and mirrored
events are excluded from the first snapshot. They require separate evidence,
transport, or operational contracts.

### Snapshot envelope

The adapter output records:

- `source_kind: frog-agents-db`;
- the operator-supplied source label;
- observed migration names as the source schema version;
- capture timestamp;
- a canonical digest of the selected normalized rows;
- repository, task, relation, agent, file, and lock observations;
- source primary keys or stable external IDs for idempotent matching.

Rows are ordered by stable keys before digesting. JSON columns are decoded and
validated. Timestamps must include a timezone. Unknown migrations, malformed
JSON, duplicate identities, unresolved required relations, and non-read-only
connections fail closed.

### Authority boundary

Imported tasks and locks remain external observations. In particular:

- an imported `in_progress` task does not activate a local Change Set;
- an imported active lock does not block or grant a local claim;
- an imported `done` task does not become reviewed, merged, closed, or ready;
- an imported target run does not become evidence;
- an imported actor, box, or repository key does not become trusted identity;
- repeated import of the same source label and snapshot digest is idempotent;
- a later snapshot supersedes the source observation without rewriting prior
  receipt history.

An application service may later let an operator explicitly seed plan intent
from a verified snapshot. That promotion must record the source identity and
snapshot digest and must still require a new local Change Set and claim before
work begins.

### One-way native task promotion

Native task promotion is a separate, explicit checkpoint before Change Set
promotion. An operator selects a dependency- and conflict-closed set of Frog
task slugs and supplies the canonical native scopes for every actionable task.
WeftMark records the source label, immutable snapshot digest, selected task
identities, normalized native intent, relations, terminal skips, and
source-satisfied dependencies before creating native task records.

The mapping is intentionally conservative:

- Frog `idea` may seed native `idea`; every other actionable source status
  seeds native `todo`;
- terminal Frog tasks are recorded as skipped observations and never seed
  native completion;
- source assignments, agents, locks, Git state, and timestamps do not become
  native ownership, authority, lineage, or evidence;
- dependencies on terminal source tasks are recorded as source-satisfied,
  while actionable dependencies and conflicts must be included in the selected
  set so that the imported graph is not silently truncated;
- the same source label is bound to one reviewed snapshot and selection until
  an explicit drift reconciliation replaces that checkpoint;
- exact retries recover partial creation and do not reset existing native task
  lifecycle state;
- tasks marked as originating from WeftMark are refused, preventing a future
  publish-to-Frog adapter from feeding its own output back into native intent;
- task prose that still looks secret-bearing is refused even though the Frog
  snapshot adapter normally redacts it earlier.

Promotion creates task intent and plan relations only. It neither creates a
Change Set nor acquires a claim. Those remain later, independently auditable
local authority transitions.

## Concepts not migrated by default

The following are implementation or deployment choices, not WeftMark domain
contracts:

- the `AGENTS.db` table layout, integer row IDs, SQLite transaction strategy,
  and `/data/src` default location;
- Frog's coupled `workflow_status` and `git_status` vocabulary;
- PID/hostname liveness as proof of worker identity;
- automatic mutation of task state after running discovered build targets;
- cached target success as current evidence without exact-head validation;
- generic event replay as a substitute for validated domain reconstruction;
- SSH workspace routing, hooks, TUI rendering, GitHub provider outbox, and
  notification delivery;
- `--force` semantics that can silently widen authority;
- an assumption that a repo, unit, task, lock, Change Set, and handoff share a
  one-to-one lifecycle.

## Replacement checkpoints

Frog remains the workspace coordination authority until WeftMark has concrete
evidence for each required surface. The dependency order is:

1. parse and validate a read-only Frog snapshot;
2. persist idempotent imported plan observations without granting authority;
3. expose plan/task listing, relations, and eligibility beside Change Set
   runtime status;
4. promote a reviewed Frog task graph into native task intent without importing
   source runtime authority;
5. dogfood explicit promotion from native/imported intent into a claimed Change
   Set;
6. expose the same application services through MCP;
7. exercise a real cross-agent handoff and recovery path;
8. compare collision refusal, stale leases, audit, task-next, and completion
   behavior against Frog before changing workspace policy.

Passing WeftMark unit tests or matching Frog command names is insufficient.
Replacement requires observed workflow parity, conservative migration, and a
recoverable transition plan.
