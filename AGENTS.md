# AGENTS.md - WeftMark working protocol

WeftMark is being designed as the durable coordination, provenance, evidence,
and review layer around human and AI software workers. Treat the repository as
an executable design record: task intent, scope, dependencies, acceptance
criteria, and required evidence should remain inspectable even when a specific
agent session disappears.

Before editing, read `README.md`, this file, and the relevant files under
`tasks/`. Do not silently widen a task. When a change touches an architectural
or behavioral contract outside the task's declared `scope.contracts`, record
that explicitly before proceeding.

## Required Frog + WeftMark dogfood

Until the Frog cutover is complete, repository work must use **both** systems
to the best of their currently shipped abilities. This is deliberate dual-run
dogfood, not automatic bidirectional synchronization.

### Authority by concern

- `tasks/*.weft.yml` is the reviewed source plan: purpose, dependencies, scope,
  acceptance/refusal criteria, and required evidence.
- Frog is the migration-period operational scheduler and lock coordinator. Its
  live task assignment and file locks must be checked before editing.
- Native WeftMark is the local authority for native task intent, Change Sets,
  semantic claims, Git observations, evidence, review decisions, and handoffs.
- Git and the configured forge are the source of commit, branch, publication,
  CI, and merge facts. Neither Frog nor WeftMark invents those facts.

An imported Frog assignment or lock is an **observation** in WeftMark, never a
native claim. A native WeftMark claim does not silently acquire or override a
Frog lock. If the two systems disagree about ownership or scope, stop and
reconcile the records; do not choose whichever answer is more convenient.

### Before editing

1. Read the source-plan task and verify hard dependencies, semantic conflicts,
   expected files, contracts, negative criteria, and evidence commands.
2. Inspect current branches/worktrees and fetch remote state when publication
   or reconciliation is in scope. Preserve unrelated work and use an isolated
   worktree for concurrent slices.
3. Inspect the Frog task and active locks. Claim the Frog task with the exact
   file set before writing. Frog locks remain mandatory during the transition.
4. Create or reuse the corresponding native WeftMark task with matching file
   and semantic scopes, then run `weftmark task claim` from the actual clean
   worktree at its exact base commit. The linked worktree shares the repository
   ledger through the common Git directory.
5. If the native claim cannot be acquired, do not bypass it silently. Record
   the failure on the Frog task, create a repair task when it exposes a product
   gap, and either fix the gap or isolate/reconcile the worktree before retrying.

Representative commands (invoke the equivalent repository virtual-environment
entry point when `weftmark` is not installed globally):

```bash
frog task info <slug>
frog task claim <slug> --agent <agent> --file <path> [--file <path> ...]

weftmark --repo <worktree> task create <slug> \
  --title "<title>" --why "<purpose>" --what "<deliverable>" \
  --priority p1 --scope file:<path> --scope contract:<contract>
weftmark --repo <worktree> task claim <slug> \
  --changeset-id <changeset-id> --claim-id <claim-id> \
  --base <exact-base-sha> --agent <agent> --session <session>
```

Use full commit identifiers in durable records even when examples show short
placeholders. Do not put prompts, credentials, tokens, or secret-bearing argv in
Frog notes, task plans, committed fixtures, or WeftMark ledger payloads.

### During implementation

- Edit only the claimed slice. If discovery requires new files or contracts,
  update the source task plus Frog and native scopes before editing them.
- Keep Frog assignments/locks and native WeftMark claims leased for the active
  session. Release or hand off work that is no longer active.
- Treat Frog imported-task selection as advisory. Promotion and native claim
  acquisition must remain explicit and idempotent.
- After each meaningful commit, refresh the native Change Set so dirty paths,
  changed paths, branch, base, and head are current. Evidence must bind to the
  exact clean head it verifies.
- ACP-driven, MCP-driven, terminal, human, Claude, and Codex workers all follow
  this protocol; the runtime adapter does not change coordination authority.

### Review, publication, and completion

1. Run the evidence commands named by the source task and the proportional full
   suite. Run `python scripts/validate_tasks.py` after task-plan edits.
2. Commit only the declared slice, refresh its Change Set, and record exact-head
   evidence with `weftmark evidence run`. A passing terminal command outside
   WeftMark is useful diagnostics but is not durable Change Set evidence.
3. Create the current review decision **before** transitioning the Change Set to
   `review`. Public/security contracts need their required review evidence; an
   author review must not be mislabeled as independent review.
4. Create or supersede a native handoff naming the exact head, next action,
   known failures, and intended receiver. Chat text alone is not a handoff.
5. Push the scoped branch, open/update its PR, and verify CI at the published
   head. Check other branches for overlap before merge or rebase.
6. Update Frog to `review`/`committed` while gates remain. Run `frog task finish`
   only after its acceptance, evidence, review, publication, and merge gates are
   genuinely satisfied; this releases the Frog lock. Do not mark a source task
   `done` merely because code exists.

Native task completion is still an evolving surface. If the current CLI cannot
express a truthful completion transition, leave the task inspectably
`in_progress` or `review`, record the exact limitation, and create/advance the
task for closing that gap instead of fabricating completion.

### Migration boundaries

- Never write Frog's database directly from WeftMark. Import verified immutable
  snapshots through the adapter/receipt workflow.
- Do not build automatic two-way status sync. Any future publish-back path must
  use an explicit, idempotent, auditable outbox with stable identity mapping.
- Snapshot/parity reports must label source, digest, capture time, import time,
  and staleness. Stale or unknown state fails safe and cannot grant ownership.
- When native WeftMark reaches parity for a workflow, document the cutover,
  prove it with cross-agent dogfood, then retire the corresponding Frog write
  path deliberately. Keep a read-only projection only as long as it is useful.

## The initial task mini-format

Initial planning lives in `tasks/*.weft.yml`. The format is deliberately a
small YAML-compatible dialect rather than a general project-management schema.
It is intended to be easy for humans to read, easy for agents to emit, and easy
to ingest later into WeftMark or Frog.

```yaml
format: weft-task-v0
phase: changesets
summary: Stable envelopes around transient agent work.

tasks:
  - slug: changeset-core-model
    title: Define the Change Set domain model
    status: todo                 # idea|todo|in_progress|blocked|review|done
    priority: P0                 # P0|P1|P2|P3
    depends:
      - domain-contracts
    conflicts:                   # optional semantic conflicts, not file locks
      - review-decision-model
    purpose: >
      Explain why the task exists and which user/operational problem it solves.
    scope:
      files:                     # optional paths/globs expected to change
        - src/weftmark/domain/**
      contracts:                 # semantic surfaces that must not drift silently
        - contract:changeset-v0
        - boundary:git-lineage
    deliverables:
      - Immutable ChangeSet identity and lifecycle types.
    accept:
      - A change set binds goal, base SHA, branch/worktree, declared scopes and state.
      - Invalid lifecycle transitions fail closed.
    negative:
      - A finished change set cannot silently move to a different base SHA.
    evidence:
      - kind: test               # test|ci|review|benchmark|deployment|security|docs
        required: true
        command: python -m pytest tests/domain/test_changeset.py
      - kind: review
        required: true
        criterion: Lifecycle invariants reviewed against docs/weftmark.mdx.
    notes:
      - Keep GitHub/GitLab specifics out of the domain object.
```

### Semantics

- `slug` is the stable task identity. Rename only when the meaning changes.
- `depends` is a hard dependency graph. A task is not claimable until all hard
  dependencies are done or an explicit exception is recorded.
- `conflicts` expresses semantic coupling. It warns that two otherwise
  file-disjoint tasks should not be executed concurrently without coordination.
- `scope.files` is an expected edit region, not permission to ignore other
  affected files discovered during implementation.
- `scope.contracts` names behavior/protocol/security surfaces such as
  `contract:evidence-v0`, `boundary:agent-identity`, or `surface:cli-review`.
  These are the seed for future semantic locks.
- `accept` is executable intent: each item should be demonstrably true.
- `negative` describes refusal rules or failure cases that must remain true.
- `evidence` says what proof is required before `done`; implementation work is
  not equivalent to verification.

The source-plan format does **not** store runtime claims, agents, sessions,
branches, evidence artifacts, or approvals. Plan files describe what should
happen; the native WeftMark ledger records what actually happened. Frog records
remain migration inputs/observations unless explicitly promoted and claimed.

Run `python scripts/validate_tasks.py` (or `make tasks`) before committing task
plan edits.

## Working rules

1. Prefer small dependency-aware slices over large feature branches.
2. A task that changes a public contract needs at least one negative/refusal
   criterion and review evidence.
3. Separate `implemented`, `verified`, `reviewed`, and `releasable`; do not use
   `done` as shorthand for all four until required evidence is satisfied.
4. Preserve provenance: commits, generated artifacts, and document revisions
   should name the source and build path that produced them.
5. Keep the domain vendor-neutral. Claude Code, Codex, OpenCode, Ollama-backed
   workers, or a human terminal are adapters/workers, not domain concepts.
6. Prefer open formats and replaceable adapters. Git and local files remain the
   lowest common denominator for the first implementation.
7. Do not add secrets, tokens, model credentials, customer data, or private
   prompts to task files or committed evidence fixtures.
8. Generated files under `build/` are disposable. The canonical documentation
   source is `docs/weftmark.mdx`; the canonical logos are the SVG files in
   `assets/`.
