# WeftMark CLI reference

WeftMark's CLI is both a human tool and a machine surface. The two rules that
govern it are:

1. **Human output may be colored and paged; machine output never is.**
2. **Repo-independent surfaces touch neither Git nor the ledger.** `--help`,
   `--version`, `completion`, `system`, `ledger verify PATH` and `bundle verify
   PATH` work outside a Git repository. `status` and the `list`/`show` commands
   still resolve a Git repository and its local ledger; outside one they fail
   with exit code 2 and a Git error.

## Vocabulary

New to this kind of tool? These are the only terms you need to start:

- **Ledger** — an append-only local record file for this clone. Not committed,
  not pushed.
- **Workspace** — your Git checkout plus its ledger.
- **Task intent** — a native `task` record: *why*, *what*, priority and scopes.
  Intent, not authority.
- **Change Set** — the envelope binding a goal, base commit, branch, declared
  scopes, Git observations, evidence and review into one reviewable object.
- **Scope** — a declared edit region: `file:src/**` or a semantic contract such
  as `contract:api-v1`.
- **Claim** — a lease on a Change Set's scopes. It makes collisions visible and
  gates agent workers.
- **Evidence** — a command result bound to an exact Git head, with a kind and a
  state (`passed`, `failed`, `unavailable`, `stale`).
- **Review** — a computed readiness decision from current evidence. `ready`
  means "evidence satisfies the declared policy", **not** "a human approved".
- **Handoff** — a continuation record for the next worker/session. Requires a
  clean worktree.
- **Frog** — the earlier RAGBAZ coordinator. Imported Frog tasks and locks are
  observations, never native WeftMark authority.

## Getting oriented

```bash
weftmark --help          # woven-W banner plus the command list
weftmark --version       # version and installed capabilities
weftmark --version --json
weftmark status          # summarize the current workspace's local records
```

Global options — `--repo`, `--ledger`, `--json`, `--no-pager`, `--producer-id`
and `--producer-kind` — must come **before** the subcommand:
`weftmark --json --no-pager status`, not `weftmark status --json`. `weftmark
<command> --help --no-pager` is a deliberate special case.

`--version` deliberately reports *capabilities*, not just a number: core Git and
JSONL ledger support, the built-in forge adapters, available completions, and
whether the optional `tui` and `sqlite` extras are installed. If `sqlite` says
`not installed`, install `.[sqlite]`; if `tui` says `not installed`, install
`.[tui]`. The optional `nostoi` CLI used by ledger attestation is reported
separately from the Python binding. These commands never open a repository or a
ledger.

## Where records live and what is safe to try

WeftMark appends records to a local JSONL ledger at
`<git-common-dir>/weftmark/ledger.jsonl` — usually `.git/weftmark/ledger.jsonl`.
The ledger is **not** Git-tracked and is never pushed; linked worktrees of one
clone share it. Git is only observed: WeftMark never commits, pushes or changes
a forge. You make commits yourself with `git`.

| Category | Commands | Effect |
| --- | --- | --- |
| Read-only | `status`, every `list`/`show`, `completion`, `system`, `ledger verify`, `bundle verify` | No ledger write, no Git write, no network. |
| Local ledger writes | `task *`, `changeset *`, `claim *`, `scope amend`, `evidence run`, `review create`, `handoff create`, `frog *`, `bundle import` | Append records locally only. |
| Does more than append | `evidence run` executes the command you pass in the worktree; `runtime start` launches a configured agent provider; `ledger attest` signs with your SSH key | Read the help before first use. |

## Color

Color is enabled only for a real terminal. Precedence, highest first:

| Variable | Effect |
| --- | --- |
| `NO_COLOR` (any value) | disable, always |
| `WEFTMARK_COLOR=never\|auto\|always` | explicit preference |
| `FORCE_COLOR`, `CLICOLOR_FORCE` (non-empty, not `0`/`false`) | force on |
| `TERM=dumb`, `CLICOLOR=0` | force off |
| default | on only when stdout is a TTY |

The palette follows the logo: dark and light blue for the woven W, orange for
the shuttle accent. State words (`passed`, `failed`, `ready`, `changes_requested`,
...) share one palette across evidence, review, task and changeset output.

**`--json` output is never colored**, regardless of these variables, and is one
JSON document per invocation. `FORCE_COLOR=1 weftmark --json status | jq .` is
safe. State transitions write to stdout; diagnostics write to stderr.

## Pager and editor

```bash
export WEFTMARK_PAGER='less -FRX'   # fallback: $PAGER, then less -FRX
export WEFTMARK_EDITOR='code -w'    # fallback: $VISUAL, $EDITOR, then vi
weftmark --help                     # paged only on a terminal
weftmark --help --no-pager          # never page
weftmark task create draft --title 'Draft' --edit
```

Pager and editor values are split with shell-like quoting rules and executed as
an argv list — never through a shell. An empty `WEFTMARK_PAGER` or `PAGER`
disables paging. If a pager cannot be started, the text is printed directly so
output is never lost.

`task create --edit` opens a short template with `why:`, `what:` and `roi_note:`
sections, then creates the task from what you save. If the editor fails or the
required fields are left blank, the command exits with code `2` and an error
naming the missing fields and the fix.

## Shell completion

Both scripts are generated from the live parser, so new subcommands and options
appear automatically. They cover nested subcommands, option flags and enumerated
values.

```bash
# bash
eval "$(weftmark completion bash)"
# fish
weftmark completion fish > ~/.config/fish/completions/weftmark.fish
```

Completion generation never touches Git, the ledger, or the network.

## JSON for scripts

Add `--json` before the command. Responses share an `ok` boolean, but `ok`
means "the command ran", not "you were approved":

```bash
weftmark --json task list | jq -r '.tasks[] | [.id, .state, .title] | @tsv'
weftmark --json evidence list --changeset my-change | jq '.evidence[] | select(.state=="failed")'
```

Successful commands emit `{"ok": true, ...}` on stdout. Policy and evidence
refusals emit `{"ok": false, ...}` with a payload such as `review`, `evidence` or
`scope_audit` plus a non-zero exit code. Invocation and Git errors emit
`{"ok": false, "error": "..."}`. Human-readable diagnostics go to stderr. Exit
codes:

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | invalid invocation, missing required input, or a failed local precondition (including "not a Git repository") |
| 3 | named record not found |
| 4 | ledger failure (including verification) |
| 5 | lifecycle/policy refusal |
| 6 | required evidence failed |
| 7 | required evidence unavailable |
| 8 | claim/scope conflict |
| 9 | bundle failure |

WeftMark is an evidence ledger, not a task tracker with fuzzy ownership: claims,
evidence and reviews are recorded with exact Git heads. For the release
engineering backlog and packaging review, maintainers use
[`docs/ENGINEERING-REVIEW-CLI-TUI.md`](ENGINEERING-REVIEW-CLI-TUI.md); it is not
end-user documentation.
