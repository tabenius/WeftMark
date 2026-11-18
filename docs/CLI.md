# WeftMark CLI reference

WeftMark's CLI is both a human tool and a machine surface. The two rules that
govern it are:

1. **Human output may be colored and paged; machine output never is.**
2. **Every read-only surface works outside a Git repository.**

## Getting oriented

```bash
weftmark --help          # woven-W banner plus the command list
weftmark --version       # version and installed capabilities
weftmark --version --json
weftmark status          # summarize the current workspace's local records
```

`--version` deliberately reports *capabilities*, not just a number: core Git and
JSONL ledger support, the built-in forge adapters, available completions, and
whether the optional `tui` and `sqlite` extras are installed. If `sqlite` says
`not installed`, install `.[sqlite]`; if `tui` says `not installed`, install
`.[tui]`. The optional `nostoi` CLI used by ledger attestation is reported
separately from the Python binding. These commands never open a repository or a
ledger.

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

Add `--json` before the command. Success and failure share a stable envelope:

```bash
weftmark --json task list | jq -r '.tasks[] | [.id, .state, .title] | @tsv'
weftmark --json evidence list --changeset my-change | jq '.evidence[] | select(.state=="failed")'
```

Successful responses contain `{"ok": true, ...}` on stdout; failures contain
`{"ok": false, "error": "..."}` on stdout with a non-zero exit code. Human-readable
diagnostics go to stderr. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | invalid invocation or missing required input |
| 3 | named record not found |
| 4 | ledger verification failed |
| 5 | lifecycle/policy refusal |
| 6 | required evidence failed |
| 7 | required evidence unavailable |
| 8 | claim/scope conflict |
| 9 | bundle failure |

WeftMark is an evidence ledger, not a task tracker with fuzzy ownership: claims,
evidence and reviews are recorded with exact Git heads. See
[`docs/ENGINEERING-REVIEW-CLI-TUI.md`](ENGINEERING-REVIEW-CLI-TUI.md) for the
existing CLI/TUI review.
