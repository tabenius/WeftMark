# Change Set scope amendment (v0)

`ChangeSet.amend_scope()` lets an operator explicitly widen an already-claimed
Change Set's declared scope, recording why. It is the deliberate, audited
alternative to two bad options: silently working outside declared scope, or
abandoning and re-claiming under a wider — and now less precise — scope
declared up front before the actual shape of the work was known.

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

`weftmark scope amend <changeset_id> --scope SCOPE [--scope SCOPE ...]
--reason "..."` persists the widened `ChangeSet.scopes` through the same
ledger path every other Change Set mutation uses. `scope audit` and
`review create` both read `declared_scopes` from the Change Set's *current*
persisted `scopes` at call time (`LocalWorkflowService.audit_scope`), so a
subsequent audit or review immediately reflects the amendment — no separate
plumbing was needed for the audit/review path to pick it up.

## Refusals

- Re-declaring an already-covered scope is refused (`added_scopes are
  already declared`) rather than silently accepted as a no-op.
- An empty `--reason`, or no `--scope` at all, is refused.
- Amending scope outside the `active`/`review` lifecycle states is refused,
  matching every other lineage-adjacent mutation (`rebase`, `advance_head`,
  `move_branch`).

## Origin

Found during real dogfood work in this repository: an operator (an agent
session) forgot to declare a native task's own `tasks/*.weft.yml` file in its
claimed scope, then legitimately needed to touch that file to close the
task out — twice, in the same session, even after already knowing about the
gap once. The only available recovery at the time was leaving the Change Set
at `review` forever with a documented but unresolved scope-audit finding.
