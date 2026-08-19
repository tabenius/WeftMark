# Handoff context budget contract v0

WeftMark handoffs are durable, lossless continuation records. Context budgeting applies when a handoff is materialized for a receiving worker; it does **not** delete information from the stored handoff.

## Default variant

`standard` is the default.

| Variant | Target tokens | Hard maximum | Intended use |
| --- | ---: | ---: | --- |
| `compact` | 800 | 1,200 | Cheap provider switch, familiar task, narrow next action |
| `standard` | 1,600 | 2,500 | Normal agent-to-agent or agent-to-human handoff |
| `deep` | 4,000 | 6,500 | Difficult debugging, unfamiliar provider, broad review |

Targets are planning budgets rather than tokenizer-specific byte limits. A materializer may use a provider tokenizer when available, but must never deliberately exceed the hard maximum. When no provider tokenizer is available, the v0 implementation uses a deterministic four-characters-per-token planning estimate and reports that counting method explicitly; callers must not treat the estimate as an exact provider billable-token count.

## Automatic context

All variants automatically include the durable orientation capsule:

- goal and next action;
- Change Set/task identity;
- repository, base SHA, head SHA, branch and worktree identity;
- active declared scopes;
- all known failures;
- readiness-critical evidence and review state, including referenced records that are unexpectedly unavailable to the materializer.

Known failures have no profile count limit. They are readiness-critical: if the mandatory capsule cannot fit the selected hard maximum, materialization fails closed rather than truncating failures.

The materializer spends remaining target budget on short current-state summaries, not historical transcript replay.

## Evidence and review summaries

Automatic evidence/review context is deliberately structural. It may carry identifiers, evidence kind/state, commit binding, staleness reasons, review outcome, and open-finding identifiers/severity/scopes. It does **not** automatically copy arbitrary evidence detail, review rationale, or finding rationale into a new provider prompt.

This keeps handoff cost predictable and prevents unreviewed prose from silently becoming cross-provider prompt context. The stable record IDs remain available for explicit retrieval when the receiving worker needs the detailed narrative.

The following states are readiness-critical in v0 and therefore mandatory when referenced by the handoff:

- evidence that is declared, running, failed, unavailable, or stale;
- review outcomes that are blocked, stale, evidence-incomplete, or ready-with-follow-up.

Passed/superseded evidence and fully-ready decisions are optional summaries and may be deferred to stay within the target budget.

## Retrieval policy

Large context is addressable rather than automatically portable.

| Context source | Compact | Standard | Deep |
| --- | --- | --- | --- |
| Current state / lineage | automatic | automatic | automatic |
| Scope / blockers / known failures | automatic | automatic | automatic |
| Evidence + review summaries | concise | normal | expanded |
| Changed-path list | concise | normal | expanded |
| Diff | retrieval only | retrieval only | may include focused excerpts |
| Source files | retrieval only | retrieval only | retrieval only |
| Previous chat transcript | retrieval only | retrieval only | retrieval only |
| Terminal history | retrieval only | retrieval only | retrieval only |

No default variant automatically injects full chat transcripts, full source files, or terminal history.

Deep-mode diff excerpts are bounded by a separate 1,400-token sub-budget, must still fit the overall target, and are labeled as **untrusted repository content**. Compact and standard defer diffs entirely.

## Provider independence

Budgets are WeftMark policy, not model/provider settings. A provider swap may select another variant without mutating or superseding the durable handoff. This allows the same handoff to be materialized cheaply for one worker and more deeply for another.

A provider-specific tokenizer can be injected at materialization time. Token-counting policy is therefore replaceable independently from the stored handoff and independently from the runtime provider.

## Safety and failure behavior

- Mandatory identity, lineage, scope, known-failure, readiness-critical state, and next-action fields are never dropped to satisfy a token target.
- If mandatory context alone exceeds the hard maximum, materialization fails closed and requires operator intervention or a larger explicit budget.
- Referenced evidence/review records missing from materializer input are surfaced in mandatory context rather than being silently omitted.
- Evidence and review records supplied under the wrong identity/Change Set are rejected.
- Optional summaries and changed paths are included deterministically until the target is reached; remainder metadata stays deferred/addressable.
- A budget must not cause evidence provenance, staleness, failures or blockers to be silently omitted in a way that changes apparent readiness.
- Secret/credential exclusion rules from `contract:handoff-v0` continue to apply to the durable handoff before materialization.
- Arbitrary evidence/review prose is not automatically copied across provider boundaries in v0.

## Non-goals

V0 does not summarize historical conversations, move active process/PTY state between providers, or define automatic provider-specific prompt templates. Full chat, terminal history, source files, detailed evidence/review prose, and compact/standard diffs remain explicit retrieval operations.
