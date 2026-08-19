# Cline Kanban integration architecture spike

Status: reviewed design spike for `kanban-client-architecture-spike`

Upstream inspected: `cline/kanban` at commit `14e371ffcaa8a929b4d4b2d23843f17506ecd2aa` (main on 2026-08-19)

## Decision

**Do not fork Cline Kanban for the first WeftMark integration.**

Use Kanban as a replaceable execution/runtime provider behind WeftMark. Keep WeftMark authoritative for Change Sets, claims, semantic scopes, lineage, evidence, review, handoff, readiness, and audit history. Treat Kanban's board persistence and task-lane state as Kanban-local state rather than canonical workflow truth.

The preferred sequence is:

1. external adapter/sidecar against the existing Kanban runtime boundary;
2. upstream contribution if a small stable runtime-client hook is needed;
3. only then consider a tiny downstream patch;
4. do not maintain a full fork unless upstream architecture makes the required boundary impossible.

This keeps the integration smaller than a fork and preserves both systems as independently replaceable components.

## Why the no-fork path is viable

Kanban already has a useful internal split between:

- persistent workspace/board/session state;
- a runtime server exposing TRPC commands;
- a WebSocket state stream;
- a separate terminal WebSocket bridge;
- worktree/Git/diff services behind the runtime API;
- a browser UI consuming that runtime boundary.

The browser is therefore not directly coupled to `node-pty`, Git subprocesses, or JSON files. WeftMark can integrate at the same runtime edge instead of taking ownership of Kanban internals.

Kanban's npm package currently exports the shared API contract types from `src/core/api-contract`, but not a public runtime client implementation. That is sufficient to understand the contract, but a robust long-lived adapter should avoid assuming undocumented TRPC wire details forever. If the proof-of-concept works, the preferred upstream change is a small supported runtime-client export or documented integration contract, not a WeftMark fork.

## Authority split

### WeftMark owns

- Change Set identity;
- task/change dependency graph;
- claim ownership;
- semantic/file/contract scopes;
- base/head lineage recorded as coordination facts;
- actor and agent-session provenance;
- evidence requirements and evidence state;
- review findings and review verdicts;
- handoffs;
- merge/release readiness;
- durable audit/event history;
- permission decisions for future remote mutations.

### Kanban owns operational runtime state

- PTY process lifecycle;
- live terminal buffer and reconnect state;
- agent CLI launch mechanics;
- Kanban-specific runtime hooks;
- task worktree creation/deletion mechanics when the Kanban adapter is selected;
- Git diff/history rendering data used as a convenience surface;
- transient runtime/session telemetry;
- Kanban's own UI layout and local interaction state.

### Git owns

- commits, trees and blobs;
- refs and ancestry;
- actual worktree contents.

The integration must never infer that changing a Kanban card lane changes authoritative WeftMark lifecycle/readiness state.

## Upstream seam map

### 1. Task and board persistence

Primary upstream file:

- `src/state/workspace-state.ts`

Kanban persists each workspace under `~/.cline/kanban/workspaces/<workspace-id>/` using JSON files including:

- `board.json`;
- `sessions.json`;
- `meta.json`;
- an `index.json` mapping repositories to workspace IDs.

Writes use a locked/atomic filesystem helper and a monotonically increasing workspace revision. The board has Kanban-local columns `backlog`, `in_progress`, `review`, and `trash`.

**Integration rule:** do not import, mirror, or promote `board.json` into the WeftMark ledger. If Kanban's board UI is used, its state is a cache/projection or execution-local shadow. WeftMark Change Sets remain canonical.

A particularly important upstream mutation is `workspace.saveState`, which accepts the browser's workspace state and persists it with optimistic revision conflict handling. The WeftMark integration must not map that wholesale save operation to WeftMark domain mutation.

### 2. Runtime command boundary

Primary upstream files:

- `src/server/runtime-server.ts`;
- `src/trpc/app-router.ts`;
- `src/trpc/runtime-api.ts`;
- `src/trpc/workspace-api.ts`.

The runtime server mounts TRPC below `/api/trpc`. Workspace scope is selected by `x-kanban-workspace-id` or a `workspaceId` query parameter. The browser uses the same boundary.

Useful operations already exist for an adapter:

- ensure/delete a task worktree;
- load task workspace context;
- start/stop a task session;
- send session input;
- load working-copy or last-turn changes;
- load Git log/refs/commit diff;
- load Git summary;
- inspect runtime/session configuration.

**Integration rule:** the initial adapter should use only the smallest allowlist of these operations. In particular, do not expose Kanban's broad Git mutation or workspace-state save APIs as generic WeftMark client capabilities.

### 3. Worktree seam

Primary upstream files:

- `src/workspace/task-worktree.ts`;
- `src/workspace/task-worktree-path.ts`;
- `src/trpc/workspace-api.ts`.

Kanban manages per-task worktrees below `~/.cline/worktrees/`. Worktree creation is serialized with a Git-common-directory lock. The worktree layer also contains logic for ignored-path mirroring and preservation/restoration of task patches when worktrees are removed/recreated.

The runtime API provides `ensureWorktree`, `deleteWorktree`, task-context lookup, and task-scoped diff/Git queries.

**Proposed identity mapping:**

```text
Kanban taskId  = WeftMark Change Set ID
Kanban baseRef = WeftMark immutable base SHA
Kanban workspace = repository selected by the Change Set
```

Using the Change Set ID avoids an extra identity table. Using the recorded base SHA rather than a movable branch name makes worktree creation reproducible and auditable.

WeftMark should record the adapter/runtime/worktree attachment as provenance, but Kanban remains responsible for the physical worktree when this adapter is active.

### 4. PTY and agent-session seam

Primary upstream files:

- `src/terminal/session-manager.ts`;
- `src/terminal/pty-session.ts`;
- `src/terminal/agent-session-adapters.ts`;
- `src/trpc/runtime-api.ts`.

`TerminalSessionManager` owns the PTY lifecycle for command-driven agents such as Codex, Claude Code, Gemini and OpenCode. It maintains task session summaries independently from the browser. `startTaskSession` resolves the task worktree, chooses the agent, starts the runtime, captures turn checkpoints best-effort, and returns a session summary.

Kanban hooks translate agent-specific runtime activity into `running` / `awaiting_review` operational session state.

**Integration rule:** these states are telemetry, not WeftMark review/readiness. For example, Kanban `awaiting_review` means the agent/runtime wants attention; it does not mean WeftMark has a completed review verdict.

When WeftMark starts a Kanban-backed worker it should record:

```text
change_set_id
adapter = cline-kanban
kanban_workspace_id
kanban_task_id
agent_id
runtime/session identity when available
started_at / stopped_at
worktree path
base SHA
```

### 5. Terminal transport seam

Primary upstream file:

- `src/terminal/ws-server.ts`.

Kanban has a dedicated terminal WebSocket bridge. One PTY can have multiple viewers. The server keeps per-viewer restore/buffering state and implements backpressure so slow phone/browser clients cannot unboundedly flood the terminal stream.

This is valuable functionality that WeftMark should not reimplement.

**Integration rule:** initially proxy or link to Kanban's existing terminal transport. Do not copy the terminal server into WeftMark. Remote exposure must sit behind the same authenticated/TLS boundary as other control-plane traffic.

A later WeftMark UI may consume the terminal WebSocket directly through an authenticated same-origin reverse proxy, while WeftMark remains authoritative for who is allowed to obtain/use that terminal capability.

### 6. Diff and Git seam

Primary upstream files:

- `src/workspace/get-workspace-changes.ts`;
- `src/workspace/git-history.ts`;
- `src/workspace/git-sync.ts`;
- `src/trpc/workspace-api.ts`.

Kanban exposes task-scoped working-copy changes and a `last_turn` mode based on captured checkpoints. It also exposes Git log, refs, commit diff, summary, sync/checkout/discard actions.

**Integration rule:** reuse the read paths first. The diff is a presentation/source-inspection aid. A review finding created from that diff is stored in WeftMark and bound to the relevant Change Set/head revision.

Do not initially expose `checkoutGitBranch`, `discardGitChanges`, or broad sync actions through the WeftMark board. Those mutate repository state outside the WeftMark coordination policy and need explicit provenance/policy before being enabled.

### 7. Browser/runtime seam

Primary upstream files:

- `web-ui/src/runtime/trpc-client.ts`;
- `web-ui/src/runtime/use-runtime-state-stream.ts`;
- `web-ui/src/runtime/workspace-state-query.ts`.

The browser uses TRPC over `/api/trpc`, scoped by `x-kanban-workspace-id`. It receives current runtime state through `/api/runtime/ws` with snapshot/update messages and reconnect logic. Board saves use optimistic revisions and surface conflicts.

This separation confirms that no-fork integration can start without touching React components.

However, the current Kanban UI assumes its own workspace state shape. Therefore directly making the existing Kanban board render WeftMark Change Sets without any Kanban changes would require a lossy mirror into Kanban `board.json`, which is the wrong authority model.

**Conclusion:** use Kanban as a runtime provider first; keep the current WeftMark tablet/phone board as the authoritative UI. If users later need the exact Kanban board UI, propose an upstream external-board/provider seam rather than maintaining a fork.

## Recommended architecture

```text
                         phone / tablet / browser
                                  |
                                  v
                       WeftMark review/control UI
                                  |
                   WeftMark HTTP + future write API
                                  |
                         WeftMark application
                    authority / policy / provenance
                                  |
                      KanbanRuntimeAdapter (optional)
                         stable WeftMark interface
                                  |
                 +----------------+----------------+
                 |                                 |
         Kanban TRPC runtime                terminal WS proxy
                 |                                 |
        worktree / agent / Git                    PTY
                 |
         Codex / Claude / OpenCode / Cline
```

The adapter belongs at the application/infrastructure edge, never in WeftMark domain models.

## Proposed adapter interface

The first adapter contract should be deliberately smaller than Kanban's full API:

```text
KanbanRuntimeAdapter
  discover_or_attach_workspace(repo_path)
  ensure_change_workspace(change_set_id, base_sha)
  get_change_workspace(change_set_id, base_sha)
  start_worker(change_set_id, base_sha, agent_id, prompt, geometry?)
  stop_worker(change_set_id)
  send_worker_input(change_set_id, data)
  get_worker_summary(change_set_id)
  get_changes(change_set_id, base_sha, mode=working_copy|last_turn)
  get_git_log(...)
  get_commit_diff(...)
  terminal_capability(change_set_id, client_id)
  cleanup_change_workspace(change_set_id)
```

Every mutating call is invoked by a WeftMark application service after WeftMark policy/claim checks; browser code does not call the adapter directly.

The adapter should translate Kanban failures into a small WeftMark-facing error vocabulary such as:

```text
not_available
workspace_not_found
conflict
agent_unavailable
runtime_failed
transport_failed
permission_denied
```

Do not leak the entire TRPC error model into WeftMark domain code.

## Implementation form

### Preferred first prototype: external sidecar

Use a small optional Node/TypeScript adapter process rather than adding Node dependencies to WeftMark core.

Reasons:

- Kanban is Node/TypeScript and its API contract is published through its npm package;
- terminal transport is WebSocket-native;
- TRPC client support is native in the same ecosystem;
- WeftMark core remains Python-only and usable without Kanban;
- the adapter can pin/test compatibility against specific Kanban versions independently.

The sidecar should expose a **WeftMark-defined local protocol**, not re-export Kanban's entire API. The Python application talks only to that narrow protocol.

Suggested location during prototyping:

```text
integrations/kanban-adapter/
```

or a separate repository/package once the contract stabilizes.

### Do not proxy Kanban board persistence

The sidecar must not offer generic `workspace.saveState` or card-lane mutation. That would recreate dual authority.

### State stream

For v1, polling session summary/diff state after explicit actions is acceptable if simpler. A later adapter can subscribe to Kanban's runtime WebSocket and publish normalized operational events into WeftMark.

Candidate normalized events:

```text
worker.started
worker.activity
worker.awaiting_input
worker.exited
runtime.worktree_ready
runtime.changed
runtime.transport_error
```

These events may update operational/session projections but must not silently manufacture WeftMark evidence or review verdicts.

## Existing WeftMark HTTP boundary

WeftMark already exposes read-only:

```text
GET /healthz
GET /v0/kanban
GET /v0/kanban/changes/{change_set_id}
```

and refuses non-loopback bind in v0. This should remain the client-facing source of board truth.

The Kanban runtime adapter is a separate internal capability. Do not merge Kanban runtime control into `/v0/kanban` simply because both mention Kanban.

A future write surface should call WeftMark application services first, for example:

```text
board action
  -> WeftMark claim/start operation
  -> policy + semantic-scope checks
  -> provenance record
  -> selected runtime adapter
  -> Kanban ensure worktree/start session
```

not:

```text
board action -> Kanban move card -> infer WeftMark state later
```

## Security boundary

Kanban's runtime can start arbitrary coding-agent processes and expose interactive PTYs. Treat it as privileged execution infrastructure.

Requirements for the integration:

- Kanban runtime stays loopback/private-network scoped unless explicitly secured;
- remote browser access terminates TLS/authentication before reaching WeftMark/Kanban local services;
- the adapter receives only capabilities required for the selected Change Set;
- terminal access requires an explicit WeftMark-authorized capability, not knowledge of a task ID alone;
- broad Kanban Git mutations remain disabled from the WeftMark surface until separately reviewed;
- Kanban runtime hook state cannot mark WeftMark work reviewed/ready;
- evidence produced by an agent runtime remains distinguishable from independent CI/reviewer evidence.

## Licensing and reuse

Both WeftMark and Cline Kanban are Apache-2.0 licensed.

This spike copies no substantial upstream source. It records upstream file/function boundaries and architectural behavior only.

If later work adapts or copies Kanban source:

- preserve applicable Apache-2.0 notices;
- identify modified upstream-derived files where required;
- carry applicable upstream `NOTICE` material if present;
- record substantial reused/adapted material in `THIRD_PARTY_NOTICES.md` or an accompanying `NOTICE` file.

Normal npm dependencies retain their own package license metadata and should be represented in the dependency inventory/SBOM.

## Upstream contribution strategy

If the sidecar proves useful but feels fragile because TRPC internals are undocumented, prefer contributing one of these small seams upstream:

1. **public runtime client package/export** — a supported typed client for the existing TRPC/runtime endpoints;
2. **runtime API compatibility/version endpoint** — lets adapters reject incompatible versions cleanly;
3. **external task metadata/provider hook** — only if we later decide the stock Kanban board should render WeftMark-owned task metadata directly.

These are much smaller maintenance commitments than a fork.

Do not request an upstream WeftMark-specific domain model. The useful upstream seam should remain generic enough for other external orchestrators.

## When a downstream patch would be justified

A small patch is acceptable only if all are true:

- the sidecar cannot achieve the required operation through the runtime boundary;
- the missing seam is small and well-defined;
- an upstream contribution is impractical or not yet accepted;
- the patch does not replace Kanban persistence with WeftMark internals;
- CI continuously rebases/tests the patch against supported Kanban versions.

Examples that might justify a small patch later:

- issuing a scoped terminal capability without exposing the full Kanban UI session;
- disabling a dangerous mutation in an embedded mode;
- adding a generic external-metadata overlay to cards.

None of these currently justify a full fork.

## Explicit non-goals

The first integration does not:

- fork Cline Kanban;
- replace `board.json` with WeftMark storage;
- make Kanban card lanes authoritative WeftMark lifecycle states;
- copy the terminal server;
- copy worktree implementation code;
- expose all Kanban TRPC procedures;
- let a browser directly invoke privileged Kanban runtime actions;
- turn Kanban agent hook events into evidence/review verdicts;
- add Node/TypeScript as a mandatory WeftMark core dependency.

## Recommended next implementation slice

The next code should be a **read/execute adapter proof-of-concept**, not a UI fork.

Minimal vertical slice:

1. start/attach to a local Kanban runtime;
2. map one WeftMark Change Set ID to Kanban `taskId`;
3. use the Change Set base SHA as `baseRef`;
4. ensure/get the Kanban task worktree;
5. start one selected agent session through Kanban;
6. read its operational session state;
7. retrieve working-copy diff through Kanban;
8. expose the resulting runtime attachment/diff to WeftMark application/UI state without changing authoritative lifecycle/readiness;
9. stop the session and clean up the worktree explicitly.

Acceptance should prove that deleting all Kanban-local board state does **not** erase or corrupt the WeftMark Change Set, evidence, review, or handoff history.

## Final recommendation

**External adapter first. No fork.**

Kanban is most valuable to WeftMark as a mature execution substrate for task worktrees, agent PTYs, terminal reconnect/streaming and diff/Git inspection. WeftMark is most valuable as the durable coordination and trust plane above it.

The integration should preserve that asymmetry:

```text
WeftMark decides and records why work may happen.
Kanban efficiently makes the selected worker/worktree/terminal happen.
```

Only add upstream or downstream Kanban code changes when a concrete missing runtime seam has been demonstrated.