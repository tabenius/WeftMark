# Quickstart: your first local WeftMark loop

WeftMark is a **local ledger for software work**. It answers four questions
that Git alone does not: who was allowed to change what, what actually changed,
what evidence exists, and who decided the work was ready. It does not replace
Git, your editor, your issue tracker, or your CI. It never commits, pushes or
publishes anything for you.

This page takes about five minutes. Run the commands **in your own project
repository**, not in the WeftMark checkout, and use a repository that already
has at least one commit.

## 1. Install and orient

```bash
pip install -e .              # from a WeftMark clone; '.[tui]' is optional
weftmark --version            # works even outside a Git repo
weftmark --help --no-pager
```

Global options such as `--json`, `--repo` and `--no-pager` go **before** the
subcommand: `weftmark --json status`, not `weftmark status --json`.

## 2. Know the words

| Term | Meaning |
| --- | --- |
| **Ledger** | Append-only local record file for this clone, at `.git/weftmark/ledger.jsonl`. Not committed, not pushed. |
| **Change Set** | The envelope binding a goal, base commit, branch, declared scopes, Git observations, evidence and review. |
| **Scope** | A declared edit region: `file:src/**` or `contract:api-v1`. |
| **Claim** | A lease on a Change Set's scopes; it makes collisions visible. |
| **Evidence** | A command result bound to an exact Git head. |
| **Review** | A *computed* readiness decision from current evidence. `ready` does not mean a human approved. |
| **Handoff** | A short continuation record for the next worker or session. |

## 3. The five-minute loop

```bash
# 1. Look around (read-only).
weftmark status

# 2. Declare the work.
weftmark changeset create fix-parser \
  --goal "Fix the parser crash on empty input" \
  --scope "file:src/parser/**"

# 3. Claim the declared scopes before editing.
weftmark claim acquire fix-parser --agent me --session local --lease-seconds 3600

# 4. Do the work in your editor, then commit it yourself with Git.
git add -A && git commit -m "Fix parser crash"

# 5. Refresh the Git observation and audit the declared scope.
weftmark changeset refresh fix-parser
weftmark scope audit fix-parser

# 6. Run a real command and record its result as evidence.
#    IMPORTANT: this executes the argv you pass in your worktree.
weftmark evidence run fix-parser --id fix-parser-tests --kind test --command pytest -q

# 7. Ask WeftMark whether current evidence satisfies the policy.
weftmark review create fix-parser --author me --require test

# 8. Leave a note for the next session (requires a clean worktree).
weftmark handoff create fix-parser \
  --task fix-parser --next "Push branch and open a PR" --receiver alice

weftmark status
```

Everything above writes only to your local ledger. `evidence run` is the one
step that executes a command; `runtime start` and `ledger attest` (not used
here) launch agents and sign ledger heads respectively.

## 4. Reading the result

- `status` shows each Change Set's state, readiness, claim and evidence counts.
- `evidence:A/B` means A current records out of B total; look at
  `weftmark evidence list --changeset fix-parser` for passes and failures.
- Readiness words: `unreviewed` (no review yet), `ready` (required evidence
  passed at the current head), `stale` (HEAD moved since review),
  `evidence_incomplete`, `blocked`.

## 5. Where the records went

`.git/weftmark/ledger.jsonl` in your project clone. It is not a Git-tracked
file and is never pushed. Linked worktrees of the same clone share one ledger.
`--ledger PATH` points a command at a different file.

## 6. When to use the task path instead

For dependency-aware work, use the native task path:
`task create --why ... --what ... --scope file:...` → `task next` →
`task claim` (which creates the Change Set and claim) → the same commit,
evidence and review steps → `task complete --reason ...`. Use `task complete`
to finish a task; `task transition` deliberately cannot set `done`.

## 7. Troubleshooting

| Problem | Fix |
| --- | --- |
| `fatal: not a git repository` | Run in your project repo or pass `--repo PATH`. Make sure it has a commit. |
| `scope must use kind:key form` | Use `file:src/foo.py` or `contract:api-v1`. |
| `native task claim requires declared scopes` | Add `--scope file:...` when creating the task. |
| `Evidence request has no outcome...` | The earlier evidence id was left unresolved; inspect the ledger and retry with a new `--id`. |
| `portable handoff requires a clean worktree` | Commit or discard changes, refresh the Change Set, then retry. |
| Claim conflict, exit 8 | Inspect `claim list`, then release the named claim with its agent/session or wait for expiry. |

See [`CLI.md`](CLI.md) for the full command, environment-variable and exit-code
reference.
