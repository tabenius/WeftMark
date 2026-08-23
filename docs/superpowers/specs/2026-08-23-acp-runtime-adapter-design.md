# ACP runtime adapter — design

Status: approved, 2026-08-23. Cross-cutting design note for four task-plan
slices in a new `tasks/57-runtime-providers.weft.yml` phase file. Each slice
still gets its own `docs/contracts/*.md` contract doc as it lands, per the
project's normal convention; this document exists to record the reasoning
that connects them before they're written up individually.

## Why

`src/weftmark/application/ports/runtime.py` (`RuntimePort`) shipped without a
task-plan entry and without any concrete adapter — only a `FakeRuntime` test
double exists. `kanban-write-control-bridge`'s own negative criteria already
name the blocker explicitly: *"Runtime start/stop or provider switching is
not exposed until an explicit RuntimePort provider registry exists."*
`docs/chapters/09-6-proposed-weftmark-architecture.mdx` and
`docs/handoffs/weftmark-kanban-integration-handoff.md` both call for ACP
specifically (WeftMark as ACP *client*, driving Codex/Claude/OpenCode/Cline
as subprocesses) rather than inventing a proprietary runtime protocol. This
closes that gap.

## Decisions

1. **Hand-rolled stdio JSON-RPC, not the official `agent-client-protocol`
   PyPI package.** That SDK is pre-1.0 (0.10.x) with no stability guarantee,
   and the adapter only needs a small slice of ACP (`initialize`,
   `session/new`, `session/prompt`, `session/cancel`, `session/update`,
   `fs/read_text_file`, `fs/write_text_file`, `session/request_permission`).
   Matches the precedent already set by `adapters/github.py`: dependency-free
   stdlib transport, fixture transport for tests. (This differs from
   `mcp-surface`'s choice to depend on the official `mcp` SDK — that SDK is
   pinned as stable v2; ACP's isn't there yet.)

2. **Adapter owns worktree lifecycle, not `GitPort`.** `GitPort` is
   deliberately read-only everywhere in this codebase (repository/head/
   refs/commit/diff/status/ancestry only — no mutation). `RuntimePort`'s own
   docstring already assigns worktree ownership to the runtime
   implementation ("Implementations may own disposable worktrees, PTYs and
   agent processes"). So `git worktree add`/`remove` lives in
   `adapters/acp.py` itself, not bolted onto `GitPort`.

3. **Auto-approve fs/permission requests scoped to the worktree only.**
   `fs/read_text_file`/`fs/write_text_file` are served for paths inside the
   session's disposable worktree and refused outside it.
   `session/request_permission` options are auto-approved only when the
   agent itself marks them as read/edit-scoped to the worktree; anything
   broader (e.g. an unscoped "run a shell command" option) is auto-denied in
   v0. Matches the caution `mcp-surface` already set with evidence execution
   defaulting to dry-run, and the stated principle that "ordinary
   coordination capabilities do not imply command-execution permission."

4. **Background reader thread per session, synchronous port methods.**
   `RuntimeWorkerState` (`IDLE`/`RUNNING`/`AWAITING_INPUT`/`EXITED`/`FAILED`/
   `UNKNOWN`) is a poll-friendly state machine, and ACP's `session/update`
   notifications arrive continuously during a turn. `start_worker`/
   `send_worker_input` send the request and return once the request is
   acknowledged, without blocking for the whole turn; a background thread
   drains notifications into an in-memory snapshot; `worker_summary` is a
   cheap read of that snapshot.

5. **Provider registry is protocol-agnostic and config-driven.** Named
   providers (`codex-acp`, `claude-acp`, ...) map to a launch argv template
   plus declared capabilities, configured via CLI flags or a
   `--runtime-config` file. No plugin/discovery mechanism in v0. Registry
   tests use the existing `FakeRuntime` double, not the real ACP adapter, so
   registry correctness doesn't depend on ACP wire details.

6. **CLI is the first (and only, for v0) consumer.** `weftmark runtime
   start|stop|status|send-input`, following the established build order
   (every prior capability landed CLI-first, before HTTP control or MCP).
   Worker-session starts/stops are ledger-recorded so `weftmark status` shows
   them and a task can't be double-started.

## Task-plan slices (`tasks/57-runtime-providers.weft.yml`)

Dependency order:

1. `runtime-port-contract` — retroactive `status: done` entry for the
   already-shipped, already-tested port (`application/ports/runtime.py`,
   `tests/contracts/test_runtime_port.py`). No code changes; backfills the
   task graph so the rest of this phase has something real to depend on.
2. `acp-runtime-adapter` (depends: `runtime-port-contract`) —
   `src/weftmark/adapters/acp.py` + `tests/adapters/test_acp.py` (fixture ACP
   subprocess) + `docs/contracts/acp-runtime-adapter-v0.md`.
3. `runtime-provider-registry` (depends: `runtime-port-contract`) —
   `src/weftmark/application/runtime_registry.py` +
   `tests/application/test_runtime_registry.py` +
   `docs/contracts/runtime-provider-registry-v0.md`.
4. `runtime-cli-surface` (depends: `acp-runtime-adapter`,
   `runtime-provider-registry`, `native-task-claim-service`) — CLI commands
   in `src/weftmark/cli/main.py` + `tests/cli/test_cli_runtime.py`, wiring
   registry + adapter + `TaskClaimService` + ledger provenance together.

## Error handling

All adapter failures map onto the existing `RuntimeErrorCode` enum
(`NOT_AVAILABLE`, `WORKSPACE_NOT_FOUND`, `CONFLICT`, `AGENT_UNAVAILABLE`,
`RUNTIME_FAILED`, `TRANSPORT_FAILED`, `PERMISSION_DENIED`) — no new error
vocabulary.

## Testing

- `tests/contracts/test_runtime_port.py` stays the port conformance suite
  (unchanged).
- `tests/adapters/test_acp.py` drives the real hand-rolled JSON-RPC client
  against a small fixture ACP agent script (fake process speaking minimal
  ACP over stdio) — no live agent, no network.
- `tests/application/test_runtime_registry.py` uses `FakeRuntime`.
- `tests/cli/test_cli_runtime.py` exercises the end-to-end CLI surface
  against a real repository ledger, same pattern as `tests/cli/test_cli_task_claims.py`.

## Explicitly out of scope for this phase

- HTTP control and MCP surfaces for runtime start/stop (future work, once
  the CLI path is proven).
- A Docker or other non-ACP `RuntimePort` implementation.
- Any plugin/discovery mechanism for providers beyond static config.
- Streaming/relaying live agent output to a remote client.
