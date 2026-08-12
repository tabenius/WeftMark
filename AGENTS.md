# AGENTS.md - WeftMark working protocol

WeftMark is being designed as the durable coordination, provenance, evidence,
and review layer around human and AI software workers. Treat the repository as
an executable design record: task intent, scope, dependencies, acceptance
criteria, and required evidence should remain inspectable even when a specific
agent session disappears.

Before editing, read `README.md`, this file, and the relevant files under
`tasks/`. Do not silently widen a task. When a change touches an architectural
or behavioral contract outside the task's declared `scope.contracts`, record
that explicitly before proceeding.

## The initial task mini-format

Initial planning lives in `tasks/*.weft.yml`. The format is deliberately a
small YAML-compatible dialect rather than a general project-management schema.
It is intended to be easy for humans to read, easy for agents to emit, and easy
to ingest later into WeftMark or Frog.

```yaml
format: weft-task-v0
phase: changesets
summary: Stable envelopes around transient agent work.

tasks:
  - slug: changeset-core-model
    title: Define the Change Set domain model
    status: todo                 # idea|todo|in_progress|blocked|review|done
    priority: P0                 # P0|P1|P2|P3
    depends:
      - domain-contracts
    conflicts:                   # optional semantic conflicts, not file locks
      - review-decision-model
    purpose: >
      Explain why the task exists and which user/operational problem it solves.
    scope:
      files:                     # optional paths/globs expected to change
        - src/weftmark/domain/**
      contracts:                 # semantic surfaces that must not drift silently
        - contract:changeset-v0
        - boundary:git-lineage
    deliverables:
      - Immutable ChangeSet identity and lifecycle types.
    accept:
      - A change set binds goal, base SHA, branch/worktree, declared scopes and state.
      - Invalid lifecycle transitions fail closed.
    negative:
      - A finished change set cannot silently move to a different base SHA.
    evidence:
      - kind: test               # test|ci|review|benchmark|deployment|security|docs
        required: true
        command: python -m pytest tests/domain/test_changeset.py
      - kind: review
        required: true
        criterion: Lifecycle invariants reviewed against docs/weftmark.mdx.
    notes:
      - Keep GitHub/GitLab specifics out of the domain object.
```

### Semantics

- `slug` is the stable task identity. Rename only when the meaning changes.
- `depends` is a hard dependency graph. A task is not claimable until all hard
  dependencies are done or an explicit exception is recorded.
- `conflicts` expresses semantic coupling. It warns that two otherwise
  file-disjoint tasks should not be executed concurrently without coordination.
- `scope.files` is an expected edit region, not permission to ignore other
  affected files discovered during implementation.
- `scope.contracts` names behavior/protocol/security surfaces such as
  `contract:evidence-v0`, `boundary:agent-identity`, or `surface:cli-review`.
  These are the seed for future semantic locks.
- `accept` is executable intent: each item should be demonstrably true.
- `negative` describes refusal rules or failure cases that must remain true.
- `evidence` says what proof is required before `done`; implementation work is
  not equivalent to verification.

The source-plan format does **not** yet store runtime claims, agents, sessions,
branches, evidence artifacts, or approvals. Those belong in the future runtime
ledger. Plan files describe what should happen; WeftMark will eventually record
what actually happened.

Run `python scripts/validate_tasks.py` (or `make tasks`) before committing task
plan edits.

## Working rules

1. Prefer small dependency-aware slices over large feature branches.
2. A task that changes a public contract needs at least one negative/refusal
   criterion and review evidence.
3. Separate `implemented`, `verified`, `reviewed`, and `releasable`; do not use
   `done` as shorthand for all four until required evidence is satisfied.
4. Preserve provenance: commits, generated artifacts, and document revisions
   should name the source and build path that produced them.
5. Keep the domain vendor-neutral. Claude Code, Codex, OpenCode, Ollama-backed
   workers, or a human terminal are adapters/workers, not domain concepts.
6. Prefer open formats and replaceable adapters. Git and local files remain the
   lowest common denominator for the first implementation.
7. Do not add secrets, tokens, model credentials, customer data, or private
   prompts to task files or committed evidence fixtures.
8. Generated files under `build/` are disposable. The canonical documentation
   source is `docs/weftmark.mdx`; the canonical logos are the SVG files in
   `assets/`.
