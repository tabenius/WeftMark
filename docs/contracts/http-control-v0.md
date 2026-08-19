# HTTP control contract v0

The WeftMark HTTP control surface is an **optional mutation adapter** over existing application services. It does not own task, claim, handoff, Git, evidence, review, or lifecycle semantics.

The read-only Kanban projection remains available independently. Control is disabled unless the server is configured with a dedicated write token and one or more explicit capabilities.

## Transport boundary

V0 keeps the same transport boundary as `http-read-v0`:

- the WeftMark process binds only to loopback;
- remote/mobile access must be provided through an authenticated TLS proxy or secure tunnel;
- permissive CORS is not enabled;
- a bearer token over plaintext remote HTTP is not treated as a security boundary.

The server refuses non-loopback binding even when write control is enabled.

## Authorization

Read and write authorization are intentionally separate.

- `--token-file` optionally protects the read projection;
- `--write-token-file` is mandatory before any control provider can be enabled;
- `--write-capability` must be supplied at least once when a write token is configured;
- a request needs both the write token and the capability for its operation.

V0 capabilities are:

| Capability | Operation |
| --- | --- |
| `claim` | claim an eligible native task through `TaskClaimService` |
| `release` | release an owned semantic claim through `ClaimService` |
| `handoff` | create a clean-head handoff through `LocalWorkflowService` |

A read token does not authorize writes unless the operator deliberately configures the same secret as both tokens.

## Endpoints

### Claim task

`POST /v0/control/tasks/{task_id}/claim`

Required JSON body:

```json
{
  "change_set_id": "chg-example",
  "claim_id": "claim-example",
  "base_revision": "0123456789abcdef0123456789abcdef01234567",
  "agent_id": "worker-a",
  "session_id": "session-a",
  "lease_seconds": 600
}
```

The remote client supplies stable Change Set and claim identifiers so retries do not create new authority accidentally. The operation uses the native task's declared scopes; the browser cannot submit arbitrary lock scopes through this endpoint.

### Release claim

`POST /v0/control/claims/{claim_id}/release`

Required JSON body:

```json
{
  "agent_id": "worker-a",
  "session_id": "session-a",
  "reason": "handoff"
}
```

The existing ownership rules remain authoritative. The operation refuses a different agent/session and does not turn an expired claim into a successful release.

### Create handoff

`POST /v0/control/changes/{change_set_id}/handoffs`

Required JSON body:

```json
{
  "handoff_id": "handoff-example",
  "task_id": "task-example",
  "next_action": "Continue the failing integration test",
  "created_by": "worker-a"
}
```

Optional fields:

- `intended_receiver_id`;
- `known_failures` as an array of strings;
- `supersedes_id`.

The existing handoff rules remain authoritative, including clean-worktree requirements, Git lineage capture, secret refusal, and supersedes-chain identity.

## Idempotency

Every mutation requires an `Idempotency-Key` request header.

Successful operations append a `control_idempotency_v0` record containing:

- operation;
- target identity;
- SHA-256 of the canonical request payload;
- the safe application result.

The raw request body is not duplicated into that record.

A retry with the same key and same canonical request returns the previous result with `replayed: true`. Reusing a key for a different operation, target, or request digest returns a conflict.

Operation-specific recovery also covers the narrow crash interval after domain mutation but before idempotency recording:

- claims use explicit Change Set/claim identities and the retry-safe native claim service;
- releases recognize an already-released claim only when owner/session/reason match;
- handoffs recognize an existing handoff ID only when its durable intent matches.

V0 assumes **one WeftMark HTTP control process per ledger**. The file-locked ledger and underlying application services remain safe against other local writers, but `control_idempotency_v0` replay serialization is not advertised as a multi-process HTTP coordination protocol. Multiple HTTP control frontends should use a single writer or a future distributed idempotency service rather than independently racing the same client keys.

## Input policy

The server supplies `requested_at`; clients cannot forge mutation timestamps.

Request bodies:

- must use `application/json`;
- must be JSON objects;
- are limited to 64 KiB;
- must contain all required fields;
- must not contain unknown fields.

This is intentionally stricter than accepting a broad future-compatible object: a new field should require a contract/version decision instead of being silently ignored.

## Response

Successful mutation responses use:

```json
{
  "ok": true,
  "control": {
    "operation": "claim_task",
    "target_id": "task-example",
    "idempotency_key": "client-generated-key",
    "replayed": false,
    "result": {}
  }
}
```

The `result` is the existing application-service result, not a separate HTTP domain model.

## Failure classes

V0 uses stable coarse error codes rather than copying arbitrary internal exception prose to the client.

Typical statuses:

- `400` malformed or invalid control request;
- `401` missing/wrong write bearer token;
- `403` write token lacks the requested capability;
- `404` control is disabled;
- `409` idempotency, ownership, scope, task-state, or handoff conflict;
- `411` missing content length;
- `413` request too large;
- `415` non-JSON request;
- `428` missing idempotency key.

## Authority and audit rules

- HTTP code does not append claims, handoffs, lifecycle state, or Git changes directly.
- Every mutation passes through existing WeftMark application services.
- Semantic scope conflict rules are unchanged.
- Moving a Kanban card never changes lifecycle state by itself.
- A successful control request never promotes evidence or review readiness.
- Merge and release are not exposed by v0.
- Arbitrary ledger writes are not exposed by v0.

## Deliberate omissions

V0 does **not** expose runtime start/stop or provider switching over HTTP yet. Those operations now exist behind provider-neutral runtime application contracts, but exposing them remotely should wait until the server has an explicitly configured `RuntimePort` provider registry and a capability model for execution authority. The HTTP adapter must not manufacture execution authority merely because a board button exists.
