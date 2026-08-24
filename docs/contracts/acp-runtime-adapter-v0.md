# ACP runtime adapter v0

The ACP adapter implements `RuntimePort` by spawning an ACP-speaking agent over
newline-delimited JSON-RPC 2.0. It supports initialization, session creation,
asynchronous prompts, cancellation, session updates, scoped text-file callbacks,
and permission requests. Adapter telemetry is operational state, never evidence,
review, readiness, or lifecycle authority.

## Worktree and permission boundary

Each Change Set uses a detached disposable Git worktree at its declared base.
The adapter owns creation and removal; the read-only Git port remains unchanged.
Text reads and writes must resolve inside that worktree. Permission requests are
approved only once, only for `read` or `edit`, and only when every declared
location resolves inside the worktree. Execute, delete, move, missing-location,
outside-worktree, and persistent grants are refused or cancelled.

This is an ACP callback policy, not an operating-system sandbox. A provider
binary runs with the invoking user's OS identity and may have capabilities that
do not flow through ACP callbacks. Operators must sandbox or otherwise trust the
configured provider executable; WeftMark does not describe callback tests as
proof of process isolation.

## Worker state

Prompts run on background threads. A session is `running` until ACP returns from
`session/prompt`, then becomes `awaiting_input`. Transport failure becomes
`failed`; explicit stop becomes `exited`. Session updates refresh observation
time but do not by themselves assert that a turn completed.

## Known limits

- One process and session per Change Set/task key in one adapter instance.
- No terminal methods or ACP authentication.
- Only working-copy changes are supported, using porcelain Git status metadata.
- Worker state is process-local; the application service records durable summary
observations separately.

## Reconnectable CLI control

The public CLI uses a detached, same-user local control host per repository,
provider, and Change Set. The host owns the ACP pipes so later CLI processes can
observe, prompt, and stop the same worker. Its Unix socket lives in a mode-0700
per-user directory, is mode 0600, and checks peer credentials where the platform
provides them. Control messages are bounded to 1 MiB and strict JSON objects.
Neither prompts nor provider arguments are written to the ledger; provider
configuration is delivered to the host over its initial private stdin pipe.
