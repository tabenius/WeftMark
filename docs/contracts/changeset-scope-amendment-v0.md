# Change Set scope amendment (v0)

`ClaimService.extend_scope()` lets the operator who holds the owning claim
explicitly widen an already-claimed Change Set's declared scope, recording
why. It is the deliberate, audited alternative to two bad options: silently
working outside declared scope, or abandoning and re-claiming under a wider —
and now less precise — scope declared up front before the actual shape of
the work was known.

## Claim-gated, not changeset-gated

`ChangeSet.amend_scope()`/`WorkspaceService.amend_scope()` alone are **not**
a safe operator entry point: they widen `ChangeSet.scopes` without acquiring
a matching lock, which would leave the newly-declared scope with zero
collision protection — a different agent could concurrently acquire a claim
over that same file with no conflict detected, even though the amending
Change Set now also claims to cover it. This was found and fixed during this
capability's own independent review before it ever shipped as the operator
path.

The real entry point is `ClaimService.extend_scope(claim_id, ...)` /
`weftmark scope amend <claim_id> ...`, which:

1. verifies the caller is the claim's owning `agent_id`/`session_id`
   (`_require_owner`, the same check `renew`/`release` use);
2. verifies the claim is currently active, not expired or released;
3. acquires a new `SemanticLock` for each added scope, at the claim's
   existing lock expiry, and checks it for conflicts against every other
   active claim exactly like `acquire()` does — refusing with the same
   `ClaimConflict` if another claim already owns it;
4. only once the lock is durably recorded does it widen the underlying
   Change Set's declared scope.

`ChangeSet.amend_scope()`/`WorkspaceService.amend_scope()` remain as the
lower-level domain/application primitives `ClaimService.extend_scope()` is
built on — same layering as `WorkspaceService.transition_change_set()` being
a primitive with no claim check, composed into claim-aware operations above
it. They are not a safe way to widen scope on their own.

## What it is not

- **Not a lineage event.** It does not touch `base_sha`, `head_sha`, or
  `branch`, and is recorded in its own `scope_amendments` history, not the
  Git `lineage` list.
- **Not a rewrite.** It only ever *adds* scopes. There is no operation to
  remove or replace a declared scope; narrowing a commitment after the fact
  is not the problem this solves and would undermine the audit trail scope
  declarations exist to provide.
- **Not a widening of the native task's own immutable intent.** The native
  `TaskIntent` that originally justified the claim is untouched; only the
  Change Set's own `scopes` field widens. A task's declared scope remains the
  reviewable record of what the operator originally committed to.
- **Not automatic.** Nothing infers or silently applies a scope amendment.
  It always requires an explicit CLI invocation with a stated `--reason`.

## How it takes effect

`weftmark scope amend <claim_id> --scope SCOPE [--scope SCOPE ...] --reason
"..." --agent AGENT --session SESSION` persists both the new lock and the
widened `ChangeSet.scopes` through the same ledger path every other claim
and Change Set mutation uses. `scope audit` and `review create` both read
`declared_scopes` from the Change Set's *current* persisted `scopes` at call
time (`LocalWorkflowService.audit_scope`), so a subsequent audit or review
immediately reflects the amendment — no separate plumbing was needed for the
audit/review path to pick it up once the claim-gated path was in place.

## Refusals

- A caller who is not the claim's owning `agent_id`/`session_id` is refused
  (`claim operation requires the owning agent and session`), identical to
  `renew`/`release`.
- Amending a scope another active claim already owns is refused with the
  same `ClaimConflict` `acquire()` raises, naming the conflicting claim and
  its expiry.
- Re-declaring a scope already locked by *this* claim is refused
  (`added_scopes are already locked by this claim`) rather than silently
  accepted as a no-op.
- An empty `--reason`, or no `--scope` at all, is refused.
- Amending scope once the claim is no longer active (expired or released) is
  refused (`Claim is no longer active`).
- Amending the underlying Change Set's scope outside the `active`/`review`
  lifecycle states is refused, matching every other lineage-adjacent
  mutation (`rebase`, `advance_head`, `move_branch`).

## Origin

Found during real dogfood work in this repository: an operator (an agent
session) forgot to declare a native task's own `tasks/*.weft.yml` file in its
claimed scope, then legitimately needed to touch that file to close the
task out — twice, in the same session, even after already knowing about the
gap once. The only available recovery at the time was leaving the Change Set
at `review` forever with a documented but unresolved scope-audit finding.
