# Kanban projection contract v0

WeftMark may be presented through Cline Kanban, another board UI, a tablet/PWA client, or a future native Android client. Those surfaces must not become alternate authorities for coordination state.

This contract defines the first stable, read-only projection intended for external board clients.

## Authority

- WeftMark owns Change Set lifecycle, claims, scope conflicts, evidence, review, handoff, and readiness semantics.
- Git remains authoritative for repository objects and ancestry.
- A board client owns only presentation and transient interaction state.
- The projection is derived from `StatusService`; producing it must not refresh Git state or mutate the ledger.
- Native task intent remains separate from Change Set lifecycle. Additive `plan_cards`
  and `task_change_set_links` fields expose the relationship without copying
  lifecycle, evidence, or readiness authority onto a task card.

Schema identifier:

```text
weftmark.kanban-projection.v0
```

## Board lanes

The projection deliberately exposes fewer lanes than WeftMark has semantic states:

| Lane | Meaning |
| --- | --- |
| `backlog` | planned but not active |
| `active` | currently executing / being changed |
| `review` | review-stage work that is not currently releasable |
| `ready` | review-stage work with `ready` or `ready_with_follow_up` readiness |
| `done` | merged, closed, or abandoned terminal work |

A lifecycle state unknown to the v0 projection maps to `review` and receives `unknown_lifecycle_state` attention. Older clients must therefore fail safe rather than accidentally treating a new state as complete.

Native task cards use the same presentation lanes but retain `task_state`:

| Native task state | Lane |
| --- | --- |
| `idea`, `todo` | `backlog` |
| `in_progress` | `active` |
| `blocked` | `review` |
| `done`, `abandoned` | `done` |

The lane is still derived presentation. It is never a task transition request.
An in-progress task without a durable work binding receives
`missing_change_set_link`; a binding to an absent Change Set receives
`missing_change_set`. Neither case causes the projection to invent authority.

## Plan sources and Change Set links

`plan_cards` contain native Task Intent, dependency and conflict identities, and
one or more provenance sources. Directly-created intent is labelled
`native-ledger`. Reviewed source-plan imports and Frog snapshot imports retain
their portable source label and digest; imported Frog assignments, locks, and
workflow status remain observations and are not projected as native authority.

`task_change_set_links` are derived only from durable native work bindings:

```json
{
  "task_id": "fix-auth",
  "change_set_id": "chg-fix-auth",
  "claim_id": "claim-fix-auth",
  "binding_state": "completed"
}
```

A linked Change Set continues to appear as its own card with lifecycle,
readiness, evidence, claims, review, and Git observation. Consumers should join
by identifier when they want a combined view; they must not infer those values
from the task lane.

## Scope collisions

Each card has a `scope_collisions` array derived by WeftMark from declared Change Set scopes and **other active claims**. A collision means that acquiring the card's declared scope would currently conflict with an existing owner.

The relation is deliberately asymmetric. A Change Set is not reported as colliding with its own claim, and WeftMark does not manufacture an impossible state in which two overlapping claims both acquired successfully. Released and expired claims do not appear as blockers.

A collision exposes only the coordination facts a board needs:

```json
{
  "claim_id": "claim-owner",
  "competing_change_set_id": "chg-owner",
  "requested_scope": {"kind": "contract", "key": "tenant-auth"},
  "owned_scope": {"kind": "contract", "key": "tenant-auth"}
}
```

This allows two file-disjoint changes to visibly conflict when they both affect the same contract, schema, boundary, or other canonical scope. The board must not independently recompute overlap rules.

## Attention flags

V0 may emit:

- `dirty_worktree`
- `obsolete_evidence`
- `failed_evidence`
- `unavailable_evidence`
- `scope_collision`
- `blocked`
- `evidence_incomplete`
- `stale_review`
- `stale_handoff`
- `unknown_lifecycle_state`

Evidence failure/unavailability is surfaced independently of formal readiness so a client can warn about a failing or missing proof before a review decision exists. `scope_collision` similarly surfaces coordination blocking independently of lifecycle or review state.

Attention flags are hints for presentation. They never replace authoritative lifecycle/readiness or claim state.

## Payload

Example:

```json
{
  "schema": "weftmark.kanban-projection.v0",
  "generated_at": "2026-08-19T12:00:00+00:00",
  "authority": {
    "coordination": "weftmark",
    "projection": "read_only"
  },
  "counts": {
    "cards": 1,
    "plan_cards": 1,
    "total_cards": 2,
    "active_claims": 1,
    "expired_claims": 0,
    "released_claims": 0
  },
  "task_change_set_links": [
    {
      "task_id": "fix-auth",
      "change_set_id": "chg-01",
      "claim_id": "claim-fix-auth",
      "binding_state": "completed"
    }
  ],
  "plan_cards": [
    {
      "kind": "task",
      "id": "fix-auth",
      "title": "Fix tenant authentication",
      "lane": "active",
      "task_state": "in_progress",
      "priority": "p0",
      "created_at": "2026-08-19T11:00:00+00:00",
      "updated_at": "2026-08-19T11:30:00+00:00",
      "planning": {"dependencies": [], "conflicts": []},
      "sources": [
        {"kind": "source_plan", "label": "workspace/tasks", "digest": "sha256:..."}
      ],
      "change_set_ids": ["chg-01"],
      "attention": []
    }
  ],
  "cards": [
    {
      "kind": "change_set",
      "id": "chg-01",
      "title": "Fix tenant authentication",
      "lane": "active",
      "lifecycle_state": "active",
      "readiness": "unreviewed",
      "git": {
        "branch": "weft/chg-01",
        "head_sha": "91f...",
        "observed_at": "2026-08-19T12:00:00+00:00",
        "dirty_paths": []
      },
      "claims": {
        "active_ids": []
      },
      "scope_collisions": [
        {
          "claim_id": "claim-owner",
          "competing_change_set_id": "chg-owner",
          "requested_scope": {"kind": "contract", "key": "tenant-auth"},
          "owned_scope": {"kind": "contract", "key": "tenant-auth"}
        }
      ],
      "evidence": {
        "total": 2,
        "current": 2,
        "obsolete": 0,
        "failed": 0,
        "unavailable": 0
      },
      "review": null,
      "handoff": null,
      "attention": ["scope_collision"]
    }
  ]
}
```

## Change Set detail: runtime identity

The single-Change-Set detail route (`GET /v0/kanban/changes/{id}`) returns the
same Change Set card as the workspace projection, plus two additive surfaces the
board projection omits to keep its card face lean. Both expose *runtime
identity* — who or what is (or was) working the change set — which the board
deliberately does not compress onto the card.

- `evidence_refs` — the change set's evidence distilled to what a detail view
  needs, one entry per evidence record:

```json
{
  "id": "ev-1",
  "kind": "test",
  "state": "passed",
  "producer": {"kind": "worker", "id": "sylvae:run/abc123"},
  "artifacts": ["sylvae://run/abc123"]
}
```

`producer.id` and `artifacts` are the seam through which the worker that
recorded the evidence — an OpenCode session, a Sylvae run, CI, or a human —
identifies itself. WeftMark keeps `producer.id` an opaque string; the
namespacing convention (e.g. `sylvae:run/…`, `opencode:session/…`) belongs to
the producing tool, and resolving it to a link belongs to the consumer. The
board (`GET /v0/kanban`) does not carry `evidence_refs`; only the detail route
does.

On the write side, a caller attributes the evidence it records with the
global `--producer-id` / `--producer-kind` options
(`weftmark --producer-id sylvae:run/<id> evidence run …`). The default is the
plain `worker`/`weftmark-cli` producer, so existing callers are unaffected; the
id is stored verbatim and never interpreted by WeftMark.

- `claims.active` — the change set's currently-active claims, each distilled to
  the runtime identity of the worker holding it. This answers "what is working
  on this **right now**", before any evidence exists, where `evidence_refs` only
  answers "what has run against it". The board's `claims.active_ids` (the claim
  id list) is unchanged; `claims.active` is added only on the detail route:

```json
{
  "id": "claim-1",
  "agent": "worker-1",
  "session": "opencode:session/ses_abc123"
}
```

`session` (and `agent`) come straight from the semantic claim's own fields.
As with an evidence `producer.id`, WeftMark keeps the `session` string opaque —
the `sylvae:run/…` / `opencode:session/…` namespacing is the claiming tool's
convention and resolving it to a link is the consumer's job. A tool that stamps
its session id with a namespace when it `task claim`s a change set lets a
consumer light the runtime link during active work, before the first evidence.

## Versioning

V0 consumers must ignore unknown object fields and unknown attention-flag strings.
`plan_cards`, `task_change_set_links`, their count fields, the Change Set
card `kind` discriminator, the detail route's `evidence_refs` field, and the
detail route's `claims.active` list are
additive: legacy `cards` remain Change Set cards, `counts.cards` retains its
original meaning, and `claims.active_ids` is unchanged. Existing fields and known
values must not silently change meaning. A semantic change to lane derivation,
authority, readiness interpretation, or scope-overlap meaning requires a new
schema identifier.

## Deliberate omissions

V0 does not yet expose:

- worker/agent runtime identity *on the board projection* (the detail route now
  surfaces it additively via `evidence_refs` for past runs and `claims.active`
  for the workers holding the change set now, see above);
- terminal endpoints;
- diff endpoints;
- mutation operations;
- task completion or claim actions from lane movement;
- HTTP transport details.

Those are separate integration slices. The first objective remains to keep an external board a replaceable projection of WeftMark rather than a second database.
