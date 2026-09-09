# Frog to WeftMark cutover runbook

This is the operational sequence for moving workspace coordination authority
from Frog to WeftMark, and for reversing that move. It operationalizes the
`contract:frog-transition-map-v0` semantics recorded in
[`docs/frog-transition.md`](frog-transition.md); where the two disagree, the
transition map defines meaning and this runbook defines procedure.

The document assumes one operator with write access to both systems, at least
two coordinating agents, and a single logical Frog workspace identified by a
stable source label. It does not authorize any write to a Frog database, and
nothing here converts a passing test suite into a cutover decision.

## Read this before planning a date

**The automated cutover gate cannot pass today, and no sequence of operator
actions in this runbook will make it pass.** `weftmark frog parity` classifies
two required checks as `unavailable` unconditionally, so
`FrogParityReport.cutover_ready` is structurally false and the command exits
`5` (`EXIT_POLICY`) rather than `0` on every input.

This is deliberate fail-closed behavior, not a defect and not a threshold to be
tuned. The two checks are the ones that cannot be honestly derived from a
snapshot's shape:

- `collision_refusal` — the snapshot records currently held locks. Refused
  acquisition attempts are absent from both the Frog receipt and the native
  ledger, so an empty current-overlap set cannot prove either system *refused*
  a competing claim.
- `completion_gate` — a terminal Frog task row records that a status changed.
  Finish verification events and target runs are excluded from the receipt, so
  the row cannot prove `frog task finish` actually ran its build, audit,
  review, and release gates.

Treating either absence as a pass would make the cutover decision rest on
missing data. Stages 0 through 3 below are available now and are worth running
on their own merits; Stage 4 requires a prerequisite that does not yet exist.

### Prerequisite for Stage 4

A parity-evidence adapter must supply bounded, provenance-bearing **refusal
receipts** and **finish receipts** from both systems, after which
`_collision_check` and `_completion_check` can classify from data instead of
returning a constant. Per the transition map, that adapter must not infer
either result from timestamps, status strings, or an empty current-conflict
set. Until it lands, record Stage 4 as blocked on evidence rather than on
schedule.

## Required check inventory

Every check is emitted with `required: true`; `_check()` admits no optional
checks. `cutover_ready` is true only when all of them are `match` or
`explained_difference`.

| Check | Can pass today | What blocks or explains it |
| --- | --- | --- |
| `source_freshness` | yes | `blocker` when snapshot age exceeds `--stale-after-seconds` (default 3600), otherwise `match`. |
| `task_graph` | yes | `blocker` on missing imported tasks, priority mismatches, or dependency/conflict edge drift. Imported runtime status is intentionally excluded. |
| `eligibility` | yes | `blocker` when the two schedulers disagree on selectability. A locally advanced lifecycle is an `explained_difference`, not a failure. |
| `stale_lease` | yes | Compares effective lease state, but only for tasks holding both a source lock observation and a native work binding. |
| `scope_audit` | yes | Compares declared-file coverage. Missing source declarations stay null rather than counting as covered. |
| `collision_refusal` | **no** | Hardcoded `unavailable`; requires the refusal-receipt adapter. |
| `completion_gate` | **no** | Hardcoded `unavailable`; requires the finish-receipt adapter. |

Read the report, not just the exit code. `--json` emits every check with its
`frog` and `weftmark` observations and a `detail` string explaining why the
classification was chosen; the five passable checks are diagnostically useful
even while the overall verdict is pinned to blocked.

## Authority at each stage

Exactly one system is authoritative for coordination at any moment. "Dual" in
Stage 1 means dual *read*, never dual authority.

| Stage | Coordination authority | WeftMark writes | Reversible |
| --- | --- | --- | --- |
| 0 Preconditions | Frog | none | n/a |
| 1 Dual read | Frog | none | trivially |
| 2 Native-write pilot | Frog | bounded pilot slice only | yes |
| 3 Freeze | Frog (frozen) | pilot slice only | yes |
| 4 Cutover | WeftMark | all | within the retention window |
| 5 Decommission | WeftMark | all | no |

## Stage 0 — Preconditions

1. Choose the source label and keep it stable. The label binds a Frog
   workspace to one reviewed snapshot and selection; changing it silently
   forks provenance.
2. Confirm the Frog database is reachable read-only. The importer opens it
   with SQLite read-only mode and `query_only`, and fails closed on a
   writable connection.
3. Confirm both agents can reach the native ledger and agree on repository
   identity. Absolute paths are adapter configuration, never portable
   identity.
4. Record the current Frog commit and migration list. Unknown migrations fail
   the import rather than degrading it.

Exit criterion: a read-only snapshot import succeeds and its digest is
recorded.

```
weftmark frog snapshot import <agents-db-path> --source-label <label>
weftmark frog snapshot list
```

## Stage 1 — Dual read

Frog remains authoritative. WeftMark observes only. The goal is to prove that
the two systems describe the same world before either is trusted to schedule
work.

1. Import a fresh snapshot at the start of each session. Snapshots are
   immutable; a later snapshot supersedes the observation without rewriting
   prior receipts.
2. Run the parity report and read all seven checks.
3. Compare scheduler output on the same graph — `weftmark frog task next`
   against `frog task next` — and record any disagreement as a defect in
   whichever side is wrong, not as tolerance to be widened.

```
weftmark frog parity <digest> --repo-path <repo> --json
weftmark frog task list <digest> --repo-path <repo>
weftmark frog task next <digest> --repo-path <repo> --limit 5
```

Exit criteria: across at least three sessions on different days,
`source_freshness`, `task_graph`, `eligibility`, `stale_lease`, and
`scope_audit` are all `match` or a reviewed `explained_difference`, with every
explained difference written down and attributed. Disagreement that recurs
after explanation is a blocker.

Rollback: stop importing. Nothing was written to Frog and no local authority
was granted.

## Stage 2 — Native-write pilot

Frog stays authoritative for the workspace. WeftMark becomes authoritative for
one bounded, named pilot slice — typically a single repository whose tasks no
other agent will touch.

1. Promote the pilot task graph into native intent, supplying operator-approved
   native scopes for every actionable task. Imported Frog scopes are not
   authoritative and the CLI requires explicit `--scope` for this reason.
2. Work the pilot slice natively end to end: claim, evidence, review, handoff,
   lifecycle transition, claim release. A status flip that skips gates is a
   failed pilot, not a fast one.
3. Exercise the failure paths deliberately — at minimum an expired claim
   recovery and one cross-agent handoff — because these are what Stage 4 is
   actually betting on.
4. Do not mirror pilot results back into Frog. There is no publish-to-Frog
   adapter, and native promotion refuses tasks marked as WeftMark-originated
   precisely to prevent that loop.

```
weftmark frog task import <digest> --task <slug> --scope <slug>=file:src/**
weftmark frog task promote <digest> <slug> --scope file:src/**
weftmark frog task claim <digest> <slug> --scope file:src/** --agent <agent>
weftmark status
```

Exit criteria: the pilot slice completed natively with real evidence; at least
one cross-agent handoff and one recovery from an expired claim were observed;
Stage 1 checks still hold for the whole workspace, not only the pilot.

Rollback: return the pilot slice to Frog scheduling. Native records remain as
history; they were never Frog's authority to begin with. Note in the ledger
that the slice was returned, so a later reader does not mistake abandoned
native records for a completed pilot.

## Stage 3 — Freeze

A freeze exists to make the two systems comparable at a single instant. Keep
it short — hours, not days — because its cost is paid by every blocked agent.

1. Announce the freeze window to every agent and to `xyzzy`.
2. Stop new Frog claims. Let in-flight claims finish or expire; do not force
   locks open. `--force` semantics are explicitly outside the migrated
   contract because they silently widen authority.
3. When no Frog claim is active, capture the final snapshot and import it.
4. Run the parity report against that snapshot within the freshness window.

Exit criteria: no active Frog locks; final snapshot imported; parity report
generated and archived with the freeze timestamp.

Rollback: lift the freeze and resume Frog scheduling. This is the last stage
that is free to reverse.

## Stage 4 — Cutover

**Blocked until the parity-evidence adapter exists.** When it does:

1. Confirm `weftmark frog parity <final-digest>` exits `0`. A non-zero exit is
   a stop, regardless of how close the report looks or how long the freeze has
   run.
2. Archive the passing report with its snapshot digest, native ledger digest,
   and ledger sequence. That triple is the evidence that a specific comparison
   authorized this specific cutover.
3. Switch agent configuration to WeftMark as coordination authority.
4. Put the Frog database into read-only service. Do not delete it; Stage 5
   defines when and whether that happens.
5. Lift the freeze and resume work natively.
6. Watch the first full working day closely. Treat any coordination surprise as
   a rollback candidate rather than an anomaly to absorb.

Exit criteria: a working day completes natively with no coordination defect
requiring Frog to arbitrate.

Rollback: while the Frog database remains intact and no native-only work has
accumulated beyond what an operator can re-enter by hand, revert agent
configuration to Frog and restore its write access. Native records from the
cutover window stay as history. This path closes as native-only work
accumulates, which is why Stage 5 is gated on time rather than on confidence.

## Stage 5 — Retention and decommission

Frog's database is the only copy of the pre-cutover coordination history, and
the snapshot receipts are deliberately partial — target runs, generic events,
hooks, peers, and mirrored events were never imported.

- Keep the Frog database read-only for **at least 90 days** after Stage 4, and
  keep it backed up off-host under the same retention discipline as any other
  production datastore.
- Keep every snapshot receipt and parity report for the life of the WeftMark
  ledger. They are the provenance for imported intent; discarding them makes
  imported native tasks unexplainable.
- Keep the final freeze-window snapshot indefinitely. It is the boundary
  object between the two systems.
- Redact nothing in place. If a receipt is found to carry secret-bearing prose
  despite the importer's redaction, supersede it with a new snapshot and record
  the supersession; do not rewrite receipt history.

Decommission — deleting the Frog database — is a separate, explicitly approved
decision after the retention window, not the tail end of cutover. It is the
only irreversible step in this runbook.

## Success gates, in order

No gate may be satisfied by test counts, matching command names, or elapsed
time.

1. **Observation parity** — the five derivable checks agree across at least
   three sessions, with every explained difference attributed.
2. **Native execution** — a pilot slice completed with real evidence, review,
   and handoff records bound to exact heads.
3. **Cross-agent evidence** — at least one real handoff between two agents and
   one recovery from an expired claim, both observed rather than simulated.
4. **Refusal evidence** — both systems demonstrably refused a competing
   acquisition, with paired receipts. *Not yet obtainable.*
5. **Completion evidence** — a Frog finish and a WeftMark finish are shown to
   run equivalent gates, with paired receipts. *Not yet obtainable.*
6. **Clean freeze** — no active Frog claim at the final snapshot.
7. **Passing report** — `weftmark frog parity` exits `0` against that snapshot
   inside the freshness window.

Gates 4 and 5 are the parity-evidence adapter's job. They are listed here so
that a future operator can see exactly which two facts the current design
refuses to assume.

## This runbook does not authorize

- writing to a Frog database, at any stage, for any reason;
- treating an imported `in_progress` task as an active Change Set, an imported
  active lock as a local claim, or an imported `done` task as reviewed, merged,
  or released;
- relaxing `--stale-after-seconds` to make a stale snapshot pass, or otherwise
  tuning a threshold to change a verdict;
- forcing a Frog lock open to shorten a freeze;
- deleting the Frog database as part of cutover;
- deciding whether WeftMark becomes the permanent coordination authority.
  This runbook covers the mechanics of moving and reversing authority; whether
  to move it is the operator's call.
