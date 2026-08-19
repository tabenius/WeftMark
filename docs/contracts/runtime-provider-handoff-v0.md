# Runtime provider handoff contract v0

A runtime-provider handoff transfers **continuation responsibility**, not a live process. WeftMark remains authoritative for the Change Set, claims, scopes, evidence, review, handoff history, and readiness. Runtime providers remain replaceable infrastructure for worktrees, agent processes, PTYs, and operational telemetry.

## Sequence

A successful switch follows this order:

```text
validate source attachment
  -> materialize durable Handoff under selected token budget
  -> append provider_switch_requested
  -> stop active source worker
  -> attach/ensure destination workspace from the same immutable base SHA
  -> start a fresh destination worker with the materialized capsule
  -> evaluate evidence revalidation policy
  -> append provider_switch_completed
```

If a provider operation fails after the request record, append `runtime.provider_switch_failed.v0` with the failed stage. Provider error prose is not copied into the ledger.

Materialization happens **before** the source is stopped. A token-budget failure therefore has no runtime side effects.

## Identity invariants

- runtime `task_id` equals the WeftMark Change Set ID;
- source and destination Change Set IDs equal the handoff Change Set ID;
- source and destination runtime workspaces use the handoff's immutable base SHA;
- the intended destination provider ID is declared before any provider-switch side effect and verified against the destination provider's returned workspace;
- worker telemetry provider/task/change identities must match its runtime workspace.

Changing provider never changes the durable handoff, Change Set identity, base lineage, evidence state, or review/readiness state.

## Token budget

The switch selects a handoff context variant (`compact`, `standard`, or `deep`); `standard` remains the default. Materialization follows `contract:handoff-context-budget-v0`.

The ledger stores only:

- selected variant;
- counted/estimated token count and counting method;
- SHA-256 digest of the materialized context.

The materialized prompt itself is not duplicated into the runtime-switch ledger records.

## Process boundary

An active source worker is explicitly stopped before the destination worker is started. The destination is a **fresh process/session**, even when the provider happens to be the same provider with a different agent.

V0 does not serialize or migrate:

- PTY state;
- process memory;
- live sockets;
- terminal scrollback;
- provider chat/session internals.

The destination must report `running` or `awaiting_input` after startup. Otherwise the switch is recorded as failed.

## Evidence revalidation

Provider switching never promotes, passes, stales, supersedes, or otherwise mutates Evidence records.

The default policy selects non-superseded evidence carrying an environment fingerprint for **revalidation** when the provider identity changes. This is a recommendation set for the destination environment, not an automatic Evidence state transition.

A different `EvidenceRevalidationPolicy` may be injected by application composition when provider identity alone is insufficient to model environment equivalence.

## Durable provenance

The request record contains:

- switch ID and handoff ID;
- Change Set ID;
- source attachment snapshot;
- intended destination provider and agent;
- context variant/count/method/digest.

The completion record additionally contains:

- source attachment after explicit stop;
- destination attachment and new session identity;
- evidence IDs selected for revalidation.

Attachment provenance includes provider/workspace/task/Change Set/base/worktree plus agent/session and operational worker state. It is provenance only; it is not a WeftMark review verdict.

## Failure behavior

A failure record identifies the switch, handoff, Change Set, and stage. Known normalized runtime adapter error codes may also be recorded. Arbitrary exception text is deliberately excluded to reduce accidental secret/context leakage.

A failure after the source has stopped does not silently restart it. The durable request/failure pair makes the interrupted transfer visible for operator or later reconciliation.

## Non-goals

V0 does not:

- guarantee atomicity across an external process provider and the local ledger;
- migrate a live worker process;
- make Kanban, OpenHands, or another provider canonical;
- expose runtime providers directly to browser clients;
- infer evidence validity from provider success;
- merge or release a Change Set.
