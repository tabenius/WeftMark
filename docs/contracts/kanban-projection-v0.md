# Kanban projection contract v0

WeftMark may be presented through Cline Kanban, another board UI, a tablet/PWA client, or a future native Android client. Those surfaces must not become alternate authorities for coordination state.

This contract defines the first stable, read-only projection intended for external board clients.

## Authority

- WeftMark owns Change Set lifecycle, claims, evidence, review, handoff, and readiness semantics.
- Git remains authoritative for repository objects and ancestry.
- A board client owns only presentation and transient interaction state.
- The projection is derived from `StatusService`; producing it must not refresh Git state or mutate the ledger.

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

## Attention flags

V0 may emit:

- `dirty_worktree`
- `obsolete_evidence`
- `blocked`
- `evidence_incomplete`
- `stale_review`
- `stale_handoff`
- `unknown_lifecycle_state`

Attention flags are hints for presentation. They never replace the authoritative lifecycle/readiness fields.

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
    "active_claims": 1,
    "expired_claims": 0,
    "released_claims": 0
  },
  "cards": [
    {
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
        "active_ids": ["claim-01"]
      },
      "evidence": {
        "total": 2,
        "current": 2,
        "obsolete": 0,
        "failed": 0,
        "unavailable": 0
      },
      "review": null,
      "handoff": null,
      "attention": []
    }
  ]
}
```

## Versioning

V0 consumers must ignore unknown object fields. Existing fields must not silently change meaning. A semantic change to lane derivation, authority, or readiness interpretation requires a new schema identifier.

## Deliberate omissions

V0 does not yet expose:

- semantic-scope collision summaries;
- worker/agent runtime identity;
- terminal endpoints;
- diff endpoints;
- mutation operations;
- HTTP transport details.

Those are separate integration slices. The first objective is to prove that an external board can be a replaceable projection of WeftMark rather than a second database.
