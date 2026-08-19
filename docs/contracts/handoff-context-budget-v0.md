# Handoff context budget contract v0

WeftMark handoffs are durable, lossless continuation records. Context budgeting applies when a handoff is materialized for a receiving worker; it does **not** delete information from the stored handoff.

## Default variant

`standard` is the default.

| Variant | Target tokens | Hard maximum | Intended use |
| --- | ---: | ---: | --- |
| `compact` | 800 | 1,200 | Cheap provider switch, familiar task, narrow next action |
| `standard` | 1,600 | 2,500 | Normal agent-to-agent or agent-to-human handoff |
| `deep` | 4,000 | 6,500 | Difficult debugging, unfamiliar provider, broad review |

Targets are planning budgets rather than tokenizer-specific byte limits. A materializer may use a provider tokenizer when available, but must never deliberately exceed the hard maximum.

## Automatic context

All variants automatically include the durable orientation capsule:

- goal and next action;
- Change Set/task identity;
- repository, base SHA, head SHA, branch and worktree identity;
- active declared scopes;
- known failures;
- evidence and decision references.

The materializer should spend the remaining target budget on short current-state summaries, not historical transcript replay.

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

## Provider independence

Budgets are WeftMark policy, not model/provider settings. A provider swap may select another variant without mutating or superseding the durable handoff. This allows the same handoff to be materialized cheaply for one worker and more deeply for another.

## Safety and failure behavior

- Mandatory identity, lineage, scope and next-action fields are never dropped to satisfy a token target.
- If mandatory context alone exceeds the hard maximum, materialization must fail closed and require operator intervention or a larger explicit budget.
- Retrieval references remain available after truncation of optional summaries.
- A budget must not cause evidence provenance, staleness, failures or blockers to be silently omitted in a way that changes apparent readiness.
- Secret/credential exclusion rules from `contract:handoff-v0` continue to apply before any materialization.

## Non-goals

V0 does not prescribe a tokenizer, summarize historical conversations, or define automatic provider-specific prompt templates. Those belong to the later handoff materializer/runtime-provider integration.
