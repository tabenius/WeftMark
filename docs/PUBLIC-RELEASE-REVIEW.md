# WeftMark public-release readiness review

**Status:** author self-review, not an independent security audit or a Frog
workflow completion. Usability and market findings were reviewed at
`1bd6a649cb7b95b2545020292df3b20356b8df2a`; the security review was re-checked
against `de3ab49a912232882b739a5f48ac7a824bb86180`. The authoritative Frog
database lives on `kali-1`, which is offline because of hardware failure and
delayed replacement parts; no Frog lock, finish or status claim was made.
Findings below are checked against source, not against the roadmap.

## 1. Verdict

The runtime is further along than the story told by its docs. Local-first
operation is genuinely safe: Git is observed, not written; every record goes to
a local ledger that is never pushed; forge adapters are read-only; and refusals
are explicit. The three things standing between this and a comfortable public
release are (1) onboarding — a newcomer has no ordered path and too much
jargon, (2) error quality — several failures assume the reader already knows
the domain, and (3) a small number of security/provenance claims that must be
stated as boundaries rather than guarantees until the code enforces them.

None of the usability items require a wire-format change. The release can be
called an **unreviewed public prototype** once the Quickstart and corrected CLI
contract land. It must **not** be called "security-reviewed", "releasable" or a
"control plane": the security review found three release blockers (section 5),
the worst of which is a release workflow that does not enforce the project's own
declared review gate. With the posture and the contradictory claims corrected,
the remaining findings are acceptable documented limitations for a prototype;
without those corrections they are grounds for over-trust.

## 2. The market: is there anything like this?

Short answer: **there is no single established category, but there is a fast
crowd of adjacent, mostly pre-alpha projects doing overlapping work.** The
useful comparison is by question answered, not by feature checklist.

| Category | Answers | Examples (as of Nov 2026) | What they do *not* do for WeftMark users |
| --- | --- | --- | --- |
| Git + forge + CI | What changed, who published it, did CI pass | Git, GitHub/GitLab, Buildkite | Do not record intent, scope leases, human review decisions or cross-session handoffs |
| Issue trackers with agent APIs | What work was planned and assigned | Linear MCP (~60 tools), Jira Rovo MCP | Assignment is not evidence; no binding between a claim and an exact Git head |
| LLM/agent observability | What did the model and tools do, what did it cost | Langfuse (MIT, self-hostable), LangSmith, Braintrust, Arize Phoenix | Trace after the fact; do not decide what may be merged or bind a human review |
| Supply-chain provenance | What build produced this artifact, is the artifact unaltered | in-toto, SLSA, Sigstore/cosign, SBOM/AI-BOM | Explicitly do not prove model behaviour, agent authority or completeness of intent |
| Agent work ledgers / evidence (closest neighbours) | Why did this change exist, what was claimed, what proved it, who accepted it | AgentLedger, KAP, OpenShard, AgentDir, code-oz, ProofFlow, AgentProvenance, Occasio, VNX | Mostly pre-alpha/local-first; no dominant standard, no clear winner; identity and independence rules vary widely |
| Orchestrators / worker runners | Which worker ran where, what gate allowed the merge | VNX, Temporal-style durable workflows, Cloudflare Durable Objects | Optimise execution and coordination, not durable human review of intent |

Two conclusions follow. First, WeftMark's differentiator is not "record
commands" (many tools do that) but the combination of **declared scope +
explicit native claims + evidence bound to an exact Git head + a computed
review decision + a handoff**, with Frog imported only as an observation.
Second, the neighbourhood is crowded enough that the docs must say plainly what
WeftMark is *not*: not an agent runtime, not a task tracker replacement, not
SLSA provenance, not a compliance certificate. A one-paragraph "why not just
Git / Linear / Langfuse?" answer in the Quickstart is worth more than ten
architecture chapters for a first-time user.

Research notes: Linear/Jira MCP comparison (Radial, 2026-11-02); observability
comparisons (vendor docs, checked 2026-10-11 to 2026-11-10); SLSA FAQ v1.2 and
NIST agentic-governance material (evidence supports governance, it does not
authorise a commit); nearest-neighbour projects' own READMEs, several of which
label themselves pre-alpha or public alpha. These are landscape pointers, not
benchmarks or endorsements.

## 3. First-time-user usability

The runtime supports a five-minute local loop, but none of the user-facing docs
show it. The complete protocol lives in `AGENTS.md`, a *contributor* document
that assumes Frog and the design chapters. Recommended newcomer path (run in
*your project*, not the WeftMark clone, in a repository with at least one
commit):

```bash
pip install -e .
weftmark --version                 # no Git repo needed
weftmark status                    # read-only, in your project repo
weftmark changeset create my-cs --goal "Fix parser crash" --scope "file:src/parser/**"
weftmark claim acquire my-cs --agent me --session local --lease-seconds 3600
# edit, then: git add -A && git commit -m "Fix parser crash"
weftmark changeset refresh my-cs && weftmark scope audit my-cs
weftmark evidence run my-cs --kind test --command pytest -q
weftmark review create my-cs --author "$(whoami)" --require test
weftmark handoff create my-cs --task my-cs --next "Push branch and open PR" --receiver alice
```

Findings, highest impact first:

1. **`docs/CLI.md` previously claimed every read-only surface works outside a
   Git repository.** False: `status`, `task list`, `changeset list` and `tui`
   all resolve Git and fail outside a repo. *Corrected in this review commit.*
2. **No Quickstart anywhere.** `README.md` jumps from install to uv locks,
   SQLite wheels and Frog tables. Add `docs/QUICKSTART.md` with the loop above,
   a "what this is / is not" paragraph, where the ledger lives
   (`.git/weftmark/ledger.jsonl`, untracked, never pushed), and a troubleshooting
   table.
3. **Global flags must precede the subcommand, but nothing says so.**
   `weftmark status --json` fails with `unrecognized arguments`. Document the
   rule and teach it in the argparse error.
4. **Two entry paths are unexplained.** `changeset create` + `claim acquire`
   versus `task create --scope` + `task claim` (which implicitly creates the
   Change Set and claim). Claims are enforced at `claim acquire`, `runtime
   start` and `task complete`; other commands run without one. Say which path
   to use first, and that the protocol convention is stricter than the code.
5. **Hidden defaults and hidden side effects.** `--base HEAD`, 1800-second
   leases, `--kind test`, and `review create --require test` are not in help.
   `evidence run` *executes* an argv; `scope audit` refreshes Git observations;
   `ledger attest` signs with an SSH key. None of that is marked in `--help`.
6. **Misleading state display.** `status` prints `evidence:3/3` when one of the
   three records actually *failed* — "current" means non-obsolete, not passed.
   The TUI does better (`src/weftmark/tui/formatting.py:37-42`); mirror its
   `N failed` counts in CLI output and add a readiness legend.
7. **Jargon leaks into user surfaces.** `ready` is presented without the
   "computed readiness, not approval" caveat; TUI `RuntimeScreen` mentions
   Sylvae, Ephor, HITL, Dash; system output mentions Minotaur and
   `RAGBAZ_RUNTIME_STATUS`. Keep these in maintainer docs.
8. **Contradictions.** README calls WeftMark a prototype not published to
   PyPI while `release-registry.json` says `releaseEligible: true`; SQLite
   install commands differ across three docs; Python version claims disagree
   (`pyproject.toml` allows 3.14, INSTALL says 3.11–3.13);
   `docs/chapters/08-5-core-concepts.mdx` uses the obsolete `weft handoff
   create ... --to` syntax.

The docs added with the CLI ergonomics work now include a vocabulary section,
ledger location, a safe-to-try table, corrected JSON and exit-code contracts,
and the global-flag rule. The remaining top item is the Quickstart.

## 4. Actionable errors

There is no shortage of error handling — there are roughly 80 custom error
classes across `domain/`, `application/` and `adapters/`. The problem is that
many failures do not say **what failed, why, or what to do next**, and several
"successful" runs hide a blocked state.

### P0: failures that lie or go silent

| Location | Current behaviour | Required fix |
| --- | --- | --- |
| `application/evidence_runner.py:210-219`, `cli/main.py:1394-1398` | `evidence run` exits 6 with only `detail: command exited with status 1`; command output is reduced to digests | Print an `error:` line naming the id, argv, Change Set, head and cwd; add a JSON `error` field; tell the user how to see output and record a new id |
| `cli/main.py:1142-1154`, `1919-1922` | `frog parity` can exit 5 while JSON says `"ok": true` | Emit `ok: false` with an error code and the failing checks |
| `cli/main.py:733-736`, `adapters/runtime_status.py:44-46` | Bad `--snapshot` prints one line and exits 0 | Report the path and reason; return non-zero or an explicit `warning` field, never silent success |
| `cli/main.py:747-770` | `ledger verify` invalid prints `INVALID: ...` to stdout, exits 4, JSON has no `error` | Use the standard error envelope on stderr |
| `application/local_workflow.py:89-91` | A refused evidence run leaves `evidence.requested`; retrying the same id dead-ends with `Evidence request has no outcome and requires reconciliation: <id>` | Name the ledger sequence and give a repair path; ideally validate the clean worktree *before* writing intent |

### P1: cryptic messages and inconsistent contracts

- `scope must use kind:key form` → `Invalid scope '<value>': expected 'kind:key', e.g. 'file:src/foo.py' or 'contract:changeset-v0'.`
- `Task not found: nope` → name the ledger path and suggest `weftmark task list`.
- `stored claim snapshot is malformed` (and the Change Set/Task/evidence/handoff variants) → name the record id, ledger sequence and `weftmark ledger verify <path>`.
- `cannot write bundle: PermissionError` → include the path, OS reason and the fix (unused path or permissions).
- Not-found exits differ by command: `task show nope` → 3, `changeset transition nope merged` → 2. Standardise on 3.
- `system`, `ledger verify`, `frog parity`, `scope audit`, `review create` and `evidence run` each invent their own JSON shape. One envelope with `code`, `message`, `record` and `remediation` would make `--json` a real contract.
- `evidence run --command false` currently gives no failure headline; raw Git stderr is forwarded verbatim at `adapters/git_local.py:298-302`.

### Partial writes that need a repair path

Evidence intent is recorded before the clean-worktree check; `scope audit`
refreshes observations before review/handoff validation; claim amendment widens
the claim before the Change Set is updated; task completion releases the claim
before writing the completion record; source-plan import writes tasks before its
receipt. Each is an append-only ledger, so nothing is silently lost, but each
failure currently leaves a state the user cannot repair from the CLI. Validate
before writing where possible; otherwise name the id and the next command.

### Error-message style guide (adopt for all surfaces)

1. Every failure states WHAT failed, WHY, and WHAT NEXT. Include a copy-pasteable command.
2. Name the offending object: record id, scope, path, commit SHA, snapshot digest, ledger path/sequence.
3. Human and JSON surfaces carry the same `code`, `message`, `record` and `remediation`; diagnostics go to stderr.
4. Keep exit-code categories stable: 2 usage, 3 not found, 4 ledger, 5 policy, 6/7 evidence, 8 conflict, 9 bundle. Never return 0 for degraded configuration without an explicit warning.
5. No raw exception class names or raw Git stderr in user-facing sentences; keep the cause in structured fields.
6. No partial state without a named repair path.
7. Do not overload payload `ok` as an error channel; `ok: false` must mean "failed".

### Status of the error fixes

The first four P0 rows above are implemented on `fix/actionable-errors` (PR #57,
head `b1c998400cccf1be4c481e3f589a139b19939de3`): a shared additive envelope
(`code`, `remediation`, `record`) for `evidence run`, `ledger verify`, `system
--snapshot` and blocked `frog parity`. The partial-write dead end (last P0 row)
and the P1 rewrites are deliberately *not* fixed here; they need their own
scoped tasks. A passing local test command is diagnostic only — durable
exact-head evidence and hosted CI are recorded in the native Change Set.

## 5. Security and provenance review

**Verdict:** do not publish WeftMark as "security-reviewed" or "releasable" yet.
The local-first design is materially stronger than the docs suggest (positive
controls below), but three blockers must be fixed or explicitly re-scoped before
public claims are made. If the project is published only as an *unreviewed
public prototype* with the corrected claims from B2, the residual risks are
disclosed and supportable. Calling it a releasable control plane is not.

### Release blockers

| ID | Severity | Finding and exact evidence | Why it blocks / fix |
| --- | --- | --- | --- |
| **B1** | Blocker (High) | Release gate not enforced. `tasks/70-release.weft.yml:263-291` leaves `alpha-release-evidence` in `review` with `review: null` and calls that "the remaining gate"; `assurance/facts.json:11,24,37,50,63,76,90` marks every capability `"releasable": false`; `scripts/build_release_evidence.py:293-301` fails only on pytest/smoke, never on a missing or non-releasable review; `.github/workflows/release.yml:51-66` gates only on tag reachability and unpublished release; `release-registry.json:26` says `releaseEligible: true`. | A tag reachable from `main` can produce signed, published artifacts while the project's own plan says the required native review does not exist. Add a machine-checkable release-readiness validator (exact source SHA, current review decision, `is_releasable: true`, no stale required evidence, no contradictory assurance facts) invoked by both `ci.yml` and `release.yml`; make the evidence collector fail when used as a gate; set `releaseEligible: false` until the gate exists. |
| **B2** | Blocker (Medium) | Public claims contradict implementation. `docs/security.md:213` says "does not yet provide signed releases, an SBOM, or a reproducible package gate" — contradicted by `.github/workflows/release.yml:106-124` (provenance attestation, cosign-signed `release.json`), `.github/workflows/ragbaz-r2.yml:54-56` (identity-pinned mirror verification) and `scripts/build_release_evidence.py:188-234` (SBOM). `ragbaz.component.json:100-109` declares `authMethods: []`, `tokenScopes: []` and "No remote mutation surface exists in the prototype" — contradicted by `src/weftmark/http/control.py:130-145` (claim/release/handoff/review routes) and `src/weftmark/http/server.py:330-348,412-445` (write token + capabilities); `docs/security.md:121-138` documents this correctly. `README.md:24-28,54-70` calls the project a prototype with nothing releasable, versus `release-registry.json:26`. | Under- and over-trust both result. Replace the stale security statement with a precise one (what is signed, what identity is pinned, what is *not* proven: human review, identity independence, reproducible builds); describe the loopback control surface and its capability model in component metadata; choose one public posture and add a docs-consistency test with exact expected statements, not just substring presence. Add user-facing `cosign verify-blob --bundle ...` instructions to `docs/RELEASING-GITHUB.md:53-63`. |
| **B3** | Blocker (High) | Unreviewed bot can change locked build dependencies. `.github/workflows/refresh-build-lock.yml:8-20` grants `contents: write` and persists credentials; lines 24-41 run `uv lock` and push `uv.lock` directly to `main`; `.github/workflows/release.yml:70-79` consumes that lock with `uv sync --locked`; `README.md:97-100` says "review the lockfile diff". | A lockfile change can reach `main` without PR review and then be consumed by a release. Convert the workflow to open/update a PR using least-privilege credentials, require branch protection and status checks on the lock diff, pin actions to commit SHAs, and have the release validator assert the lock came from a reviewed PR. |

### High and medium findings (do not block an explicitly unreviewed prototype)

- **H1 — unsigned bundles called "verified"** (`application/bundle.py:103-124`,
  `bundle_import.py:41-76`, `cli/main.py:743-745,1834-1838`). `verify_bundle()`
  checks only envelope shape and a self-digest; anyone can construct a matching
  bundle. Say "self-consistent digest verified (unsigned bundle)" and emit
  `"authenticated": false`; require a detached signature with a pinned signer
  before an imported bundle is treated as authority. Must be fixed before
  bundles are used as release/review evidence.
- **M1 — terminal control injection in human surfaces** (`cli/main.py:1551-1561,
  1783,1804-1809,2088-2092,2101-2103,2121-2122,2171,2184`;
  `tui/screens.py:44-58,104-107`; `tui/formatting.py:55-91`). `docs/security.md:70-72`
  already classifies repository text as untrusted, but `print()`-based emitters
  pass ESC/OSC sequences through; only `adapters/runtime_status.py:40-41` and
  `tui/screens.py:136-139` sanitize today. Add one shared sanitizer and set
  `markup=False` on all TUI `Static` widgets; add regression tests with OSC 8
  hyperlink and CSI payloads. (Textual/Rich rendering not exercised locally.)
- **M2 — mutable action refs in release/CI workflows**
  (`release.yml:46,67,107,122,145,156,185`; `ragbaz-r2.yml:39`;
  `refresh-build-lock.yml:18,21`; `ci.yml:21-22,40,63,75-76,89-90,115,127`).
  These actions run with release permissions and R2 secrets. Pin every `uses:`
  to a full 40-character SHA and lint against tag refs in release workflows.
- **M3 — signed manifest metadata does not match published layout**
  (`scripts/create_registry_manifest.py:15,53-58` emits paths like
  `releases/weftmark/<ver>/multi/<name>` while `.github/workflows/ragbaz-r2.yml:117-135`
  publishes `v1/weftmark/${TAG}/<name>`; repository identity case differs across
  manifest, verifier and `release-registry.json:4`). Current in-repo verifiers
  use `name` and happen to match the case, so this is not a live publish failure,
  but external consumers of the signed manifest may not. Generate `path` from
  the real object keys and canonicalize repository identity once; add a test.

### Documented limitations that must stay visible

Actor/session/reviewer ids are provenance, not authentication (`docs/security.md:62-66`);
command evidence is not sandboxed (`application/evidence_runner.py:139-176`);
secret filtering is pattern-based (`evidence_runner.py:33-39,231-248`); the JSONL
chain is tamper-evident, not signed (`adapters/jsonl_ledger.py:60-95`); the
ledger has no quota and buffers raw output before hashing; SQL checkpoint
comparison is unauthenticated (`adapters/sql_ledger.py:268-278`); loopback HTTP
is exposure reduction, not user authentication, and the token file mode/ownership
is not enforced (`http/server.py:77-101,186-207`); `WEFTMARK_NOSTOI` selects an
arbitrary CLI (`adapters/nostoi.py:29`); ACP providers run with the operator's OS
identity and the `SO_PEERCRED` check falls back open on unsupported platforms
(`adapters/acp.py:1358-1395`); ordinary pip installs do not consume `uv.lock`
(`pyproject.toml:14,21-37`). None is a new defect, but none supports a claim of
independence, separation of duties or byte-identical downstream builds.

### Positive controls verified in source

No `shell=True`, `os.system`, `eval`/`exec`, `pickle`, `tarfile` or `zipfile` in
the Python tree; YAML uses a strict `SafeLoader` subclass with duplicate-key and
alias rejection (`adapters/weft_plan.py:172-202`). Evidence runs as an argv vector
with size/time bounds, clean worktree and cwd inside the worktree
(`application/evidence_runner.py:139-176`). The JSONL ledger uses `O_NOFOLLOW`,
symlink refusal, `0600`/`fchmod` and full-chain validation
(`adapters/jsonl_ledger.py:60-95`). HTTP control binds loopback only, uses a
constant-time bearer compare, a dedicated write token and explicit capabilities
(`http/server.py:77-92,148-153,186-237,412-445`). Forge adapters refuse
cross-origin redirects and cap response bodies (`adapters/_http.py:40-76`). The
web review surface writes with `textContent` and forces CAD approval to `none`
(`web/review/app.js:41-51,227-245`). Release integrity includes tag/ref checks,
provenance attestation, a signed manifest, identity-pinned mirror verification
and create-only R2 publication (`release.yml:28-136`, `ragbaz-r2.yml:54-102,121-143`).

### Pre-release sequence

1. Fix B1: add and enforce an exact-head release-readiness validator; make
   `releaseEligible` truthful.
2. Fix B2: rewrite the stale security statement and component metadata, add
   signature-verification instructions, and choose one public posture.
3. Fix B3: PR-based, least-privilege lock refresh with reviewed diffs.
4. Fix H1: stop calling unsigned bundles "verified".
5. Fix M1–M3: sanitize human output, pin action SHAs, align signed manifest paths.
6. Re-run this review at the final exact head; split every remediation into its
   own scoped WeftMark task rather than widening the review task.

**Evidence limitations:** no remote release artifact was fetched or verified,
no dependency CVE scan was run, Textual/Rich rendering was not exercised, and
Frog was not queried (kali-1 offline). This is an author self-review, not an
independent security audit.
