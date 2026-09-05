# Terminal review UI — design

Status: approved, 2026-09-04. Design for `terminal-review-ui`
(`tasks/50-surfaces.weft.yml`), a new read-only terminal surface for
glancing at Change Set/evidence/blocker state during solo and small-team
workflows.

## Why

`weftmark status` (CLI) and the existing tablet web review surface
(`tablet-web-read-model`, done) both already read from
`StatusService.summarize()` — a ledger-derived `WorkspaceStatus` snapshot
with per-Change-Set readiness, evidence counts, review/handoff freshness,
and scope-collision blockers. Neither surface is well suited to a fast,
glanceable, "what needs my attention right now" check from a terminal:
`status` is a flat text dump sized for scripting/piping, and the web
surface needs a browser. `terminal-review-ui`'s own accept criterion is
narrow and reuse-oriented: "The TUI consumes the same read model as
CLI/MCP" — so this is a presentation layer, not a new read model.

## Decisions

1. **Reuse `StatusService.summarize()` verbatim; no new read logic.** The
   TUI's startup sequence mirrors the CLI `status` command exactly:
   `LocalGit → LedgerService(JsonlLedger) → WorkspaceService →
   ClaimService → LocalWorkflowService → StatusService`, then one
   `summarize(observed_at=now)` call. The only I/O is parsing
   `ledger.jsonl` (819 entries in this repo today; cheap) — no independent
   git-tree walks. This satisfies `terminal-review-ui`'s negative criterion
   ("TUI startup cannot require scanning entire repository trees
   synchronously") by construction: the read path already exists, is
   already ledger-only, and is already exercised by `status`'s own tests.

2. **Textual, as an optional extra (`weftmark[tui]`), not a base
   dependency.** `pyproject.toml` currently ships `dependencies = []` with
   `mcp` as the only optional extra. Textual (`>=0.60,<1`) follows that
   exact pattern: `[project.optional-dependencies] tui = ["textual>=0.60,<1"]`.
   Base `weftmark` install stays dependency-free; a `weftmark tui`
   invocation without the extra installed prints a clear "pip install
   weftmark[tui]" message rather than an ImportError traceback. Rejected:
   stdlib `curses` (zero-dependency but materially worse widgets/styling
   for a review-focused UI, and the project already accepts optional
   extras for MCP) and making Textual a required dependency (changes the
   zero-dependency posture for every install, including CI/MCP-only use).

   **Post-implementation note:** what shipped pins `textual>=8,<9`, not
   `>=0.60,<1`. `0.60` was verified stale against live PyPI during
   implementation; `8.2.8` was the current release at the time and is what
   was installed and exercised by the test suite throughout. The optional-
   extra decision itself (this section's actual point) is unchanged.

3. **Interactive, navigable, no auto-refresh.** Two screens: a master list
   (one row per Change Set — id, `lifecycle_state`, `readiness`, evidence
   counts, active claim, blocker count — sorted blockers/attention-needed
   first) and a detail view per Change Set (full evidence list, latest
   review outcome + staleness, latest handoff, scope collisions spelled
   out as "blocked by claim X on scope Y"). `j`/`k`/arrows navigate,
   `Enter`/`l` opens detail, `Esc`/`h` goes back, `r` re-runs `summarize()`
   and redraws, `q`/`Ctrl+C` quits. No timer-based auto-refresh — this is
   a "check when I ask" tool, not a monitoring dashboard. No write actions
   anywhere: this stays a pure presentation layer, matching the task's own
   "Read-mostly" deliverable wording and avoiding a second write-model
   surface to keep consistent with CLI/MCP.

   **Post-implementation note:** the detail screen shows evidence *counts*
   (current/total, obsolete, unavailable, failed), not the "full evidence
   list (kind, state, command, bound commit)" described above. The reused
   read model — `ChangeSetStatus` from `StatusService.summarize()` — only
   carries counts, not individual evidence records; producing the full
   per-record list would need a new read call outside `StatusService`,
   which decision 1 rules out ("reuse `StatusService.summarize()` verbatim,
   no new read logic"). Counts are what shipped and are consistent with
   decision 1's constraint. Everything else in this decision (navigation,
   `detail_text` layout, no-write posture) shipped as described.

4. **"Workspace with at least 50 repositories" (the required benchmark)
   means 50 `repository_id`/worktree entries in one shared ledger, not 50
   separate `--repo` invocations.** Native WeftMark's ledger is
   per-`--repo`/per-common-`.git`-dir; there is no multi-repo enumeration
   anywhere in the native CLI (only Frog has that concept). Linked
   worktrees do share one ledger through the common Git directory, and
   this repo's own ledger already carries Change Sets against multiple
   worktree/branch values. The benchmark test builds a synthetic ledger
   fixture with 50+ such Change Sets and asserts `summarize()` plus first
   render stays under ~500ms ("interactive").

   **Post-implementation note:** the shipped benchmark
   (`tests/tui/test_benchmark.py`) asserts a 1.0s budget, not ~500ms.
   Measured actual startup on this fixture is ~0.272s — well within the
   original ~500ms figure — so the wider budget was chosen deliberately
   for headroom against a self-hosted CI runner under load, not because
   500ms turned out to be unachievable.

## Package layout

- `src/weftmark/tui/app.py` — the Textual `App` subclass, screens, and key
  bindings described in decision 3.
- `src/weftmark/tui/__main__.py` / console script `weftmark-tui` — kept
  as a separate entry point (not folded into `weftmark`'s own argparse
  tree at import time) so importing the base `weftmark` CLI never imports
  Textual. `weftmark tui [--repo ...] [--ledger ...]` in the main CLI
  becomes a thin subprocess-free dispatch into this module, guarded by a
  try/except `ImportError` that prints the install-extra message.
- No new domain or application code. `tui/` only depends on
  `weftmark.application.status` (`StatusService`, `WorkspaceStatus`,
  `ChangeSetStatus`) and the same service-construction helpers `cli/main.py`
  already uses.

## Error handling

- Invalid `--repo`, an unreadable ledger, or `StatusService.summarize()`
  raising all surface as a plain stderr error message *before* entering
  full-screen mode — same fail-fast posture as the CLI, never a blank or
  broken alternate-screen buffer.
- Textual not installed: caught at the `weftmark tui` CLI dispatch
  point, prints `pip install weftmark[tui]` and exits non-zero; never an
  ImportError traceback.

## Testing

- No new domain-level unit tests are needed — there is no new domain
  logic, only presentation over an existing, already-tested read model.
- TUI-layer tests use Textual's built-in headless harness
  (`App.run_test()` / `Pilot`), which drives key presses and asserts on
  rendered content without a real TTY, so this is fully TDD-able in CI
  like the rest of this codebase. Coverage: list rendering from a given
  `WorkspaceStatus` fixture, navigation into/out of detail, the `r`
  refresh action, and the pre-full-screen error path.
- One benchmark test (decision 4): synthetic 50+-Change-Set ledger
  fixture, asserts `summarize()` + first render completes within budget.
  This is `terminal-review-ui`'s required evidence entry.

## Explicitly out of scope for this phase

- Any write/mutating action from the TUI (claim, evidence run, review
  create, etc.) — read-only only, matching the task's own scope.
- Auto-refresh / live-watch mode — no file-watching, no polling timer.
- Multi-repo enumeration across separate `--repo` paths in one TUI session
  (see decision 4) — out of scope until native WeftMark itself gains a
  multi-repo workspace concept.
- Mouse support, theming beyond Textual's defaults, and any Windows-native
  terminal certification.
