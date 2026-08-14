# WeftMark dogfood log

This log records real development slices run through WeftMark itself. It keeps
implementation, local proof, review readiness, and portable continuation as
separate facts. A session counts toward the dogfood gate only after its exact
Change Set head has current required evidence, a review decision, and a
handoff record.

## Session 001: establish the dogfood record

- Change Set: `dogfood-001`
- Goal: record the first end-to-end WeftMark dogfood session and observed
  friction.
- Base: `028540545ea08ceb26ac7008d5f5d33e8648ccba`
- Declared file scope: `docs/dogfood-log.md` and
  `tasks/60-frog-transition.weft.yml`
- Declared semantic scope: `contract:dogfood-v0`
- Worker: Codex, using the local `weftmark` CLI
- Frog transition task: `weftmark-dogfood-session-001`
- Status: in progress

The Change Set was created before either scoped file changed. The first commit
will deliberately receive valid test evidence and then be superseded by a
documentation update. Reviewing the later head before rerunning proof must
surface the earlier evidence as stale rather than failed or current. Fresh
evidence, a scope audit, a ready decision, and a clean-head handoff will then
close the session.

### Workflow commands

The session uses these product commands from the repository virtual
environment:

```text
weftmark changeset create dogfood-001 ...
weftmark changeset refresh dogfood-001
weftmark evidence run dogfood-001 --id dogfood-001-test-1 ...
weftmark scope audit dogfood-001 --semantic-change contract:dogfood-v0
weftmark review create dogfood-001 --id dogfood-001-review-stale ...
weftmark evidence run dogfood-001 --id dogfood-001-test-2 ...
weftmark review create dogfood-001 --id dogfood-001-review-ready ...
weftmark handoff create dogfood-001 --id dogfood-001-handoff-1 ...
```

Only command metadata and output digests belong in the ledger; raw test output
and environment values do not.

### Friction observed so far

- WeftMark does not yet acquire or renew coordination claims from the CLI, so
  this session still uses Frog for the workspace lock while WeftMark owns the
  Change Set, scope, evidence, review, and handoff records.
- Durable local records live under `.git/weftmark/ledger.jsonl`, which keeps
  them out of commits but means publication and cross-machine transport remain
  future work.
- The CLI requires globally unique caller-supplied IDs. This is explicit and
  deterministic, but a safe ID-generation helper would reduce typing and
  collision risk.

### Result

The first scoped commit was
`5b6f038f55289aa2d4f18ccec863cb1a0a3b1807`. Evidence
`dogfood-001-test-1` ran the complete pytest suite successfully against that
exact head; its stdout and stderr were stored only as SHA-256 digests.

This result update intentionally creates a later Change Set head. The next
review must therefore refuse to treat `dogfood-001-test-1` as current proof.
The final head, replacement evidence, review outcomes, handoff ID, and design
corrections will be recorded after that refusal has been observed.
