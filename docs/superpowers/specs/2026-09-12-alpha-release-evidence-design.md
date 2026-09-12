# Alpha release evidence — design

Status: approved, 2026-09-12. Design for `alpha-release-evidence`
(`tasks/70-release.weft.yml`), the first release evidence bundle: a
single, honest, machine-readable artifact proving exactly what state of
the codebase the first public alpha claims to be.

## Why

`alpha-release-evidence`'s own accept criteria are specific: the bundle
must "identify exactly which commit was built and reviewed," and "missing/
unavailable evidence is visible rather than omitted" — with an explicit
negative criterion that "a release cannot claim verified capabilities
absent corresponding evidence." Nothing in the repository currently
composes version, source SHA, task graph state, test/CI evidence, a
review decision, dependency inventory, and a docs digest into one
artifact. Its three dependencies (`packaging-alpha`, `open-source-license`,
`docs-assurance-facts`) are all `done`, so this is now the only thing
standing between the current prototype and a first genuinely evidenced
alpha.

## Decisions

1. **One schema-versioned JSON file, not a directory bundle.**
   `release/release-evidence-v0.json`, tagged `"schema":
   "weftmark.release-evidence.v0"` — matching `assurance/facts.json`'s
   own `schema`-field convention exactly. A single file is easy to diff,
   hash, and upload as one CI artifact; this project has no existing
   multi-file bundle convention to extend, and inventing one here would
   be scope beyond what this task needs.

2. **The script that builds it actually re-verifies everything at
   generation time — it doesn't query external CI state.**
   `scripts/build_release_evidence.py` (matching the existing
   `scripts/build_*.py` style) runs `python -m pytest -q` and `make
   smoke` itself, embeds the real exit codes and pass counts, and records
   the exact commands run. This avoids a GitHub Actions API dependency
   (and the token/auth it would need) and matches this project's existing
   self-contained-evidence philosophy (`packaging-alpha`'s own
   `scripts/smoke_install.py` is the identical pattern: the same command
   runs locally and in CI, so the two can never silently drift).

3. **The review decision is a real native WeftMark review on a dedicated
   `alpha-release-evidence-cs` Change Set — a release-readiness
   attestation, not a review of the tooling's own code.**
   The script reads that Change Set's latest review via
   `LocalWorkflowService.list_reviews(change_set_id=...)` (the exact
   ledger-reading pattern `BundleService.export()` already uses for
   portable Change Set export) and embeds its outcome, id, and head SHA.
   The reviewer's actual job when creating that review is to look at the
   bundle's own embedded task-graph and assurance-facts rollup (decision
   4) and judge whether the aggregate evidence is sufficient to call this
   state an alpha — not merely whether `build_release_evidence.py`'s code
   is correct.

4. **The assurance-facts rollup is embedded honestly, including
   `reviewed: false` capabilities — this is what actually satisfies the
   negative criterion, not the release-readiness review alone.**
   `assurance/facts.json` already tracks `planned`/`implemented`/
   `verified`/`reviewed`/`releasable` per capability (7 facts today, most
   `verified: true`, most `reviewed: false`). The bundle copies this
   in verbatim. A single top-level "reviewed: yes" flag on the release
   Change Set would not, by itself, prove any specific *capability* has
   corresponding evidence — the per-capability rollup is what lets a
   reader see exactly which claims are and aren't backed, matching
   "missing/unavailable evidence is visible rather than omitted" at the
   capability level, not just the release level.

5. **SBOM: resolved transitive dependencies per extra, using
   `packaging-alpha`'s own venv-building pattern.** For each of the two
   real extras (`mcp`, `tui`), the script builds a fresh `uv venv`,
   installs the extra, and records `uv pip list --format json`'s full
   output — verified live this session to produce clean, parseable JSON.
   Base install is recorded as an empty list, truthfully (verified
   throughout `packaging-alpha`: zero base dependencies).

6. **Docs digest: SHA-256 of the two generated build artifacts plus the
   source they're built from.** Runs `make docs` (verified live this
   session: builds cleanly in the same environment CI uses, no extra
   setup beyond what CI's `requirements-docs.txt` already installs), then
   hashes `build/weftmark.html`, `build/weftmark_A5.pdf`, and
   `docs/weftmark.mdx` (the source of truth per this repo's own README).

7. **Version bump to `0.1.0a1` (PEP 440 alpha pre-release) as part of
   this task.** `0.0.1` reads as pre-alpha; the bundle needs a real
   version string worth calling "the first public alpha." This is the
   only place `pyproject.toml` changes in this task.

8. **`release/` is gitignored, matching `dist/`'s treatment in
   `packaging-alpha`.** The bundle embeds the exact commit SHA it was
   built from — committing it would immediately go stale (the commit
   adding it would have a different SHA than the one embedded). It's
   generated fresh and uploaded as a CI artifact only, the same treatment
   `packaging-alpha` gave the wheel.

9. **Publish as a CI artifact upload alongside the wheel, not a GitHub
   Release.** Extends the existing `validate-and-build` job: after
   `Package build and clean-install smoke test`, a new step runs
   `scripts/build_release_evidence.py`, then `actions/upload-artifact`
   uploads `dist/*.whl` and `release/release-evidence-v0.json` together.
   A real GitHub Release (tag, public announcement) is a much bigger,
   more visible, harder-to-undo action than this task's evidence-bundle
   scope calls for — explicitly out of scope (decision below).

## Package layout

- `scripts/build_release_evidence.py` — new. Assembles the bundle per
  decisions 2, 3, 4, 5, 6. Depends only on stdlib (`subprocess`, `json`,
  `hashlib`, `argparse`), `yaml` (already a `requirements-docs.txt`
  dependency, used the same way `scripts/validate_tasks.py` already
  does), and WeftMark's own `LocalWorkflowService`/`WorkspaceService` for
  reading the review decision from the local ledger.
- `.gitignore` — add `release/` (decision 8), same section as the
  existing `dist/` entry from `packaging-alpha`.
- `Makefile` — add a `release-evidence` target calling the script,
  matching the existing `smoke`/`tasks`/`figures` target pattern.
- `.github/workflows/ci.yml` — one new step in `validate-and-build`,
  after `Package build and clean-install smoke test`: `make
  release-evidence`, then `actions/upload-artifact` uploading
  `dist/*.whl` and `release/release-evidence-v0.json`.
- `pyproject.toml` — version bump only (decision 7).
- `tasks/70-release.weft.yml` — `alpha-release-evidence`'s `status: todo`
  → `review` in the implementation commit, per this repo's convention.

## Error handling

- If `pytest` or `make smoke` fails during bundle generation, the script
  still records the failure (exit code, truncated output) in the bundle
  rather than aborting silently — the negative criterion is exactly
  about not omitting missing/failing evidence. The script's own process
  exit code reflects overall success, so CI still fails loudly if the
  underlying evidence is bad; only the *bundle's contents* are permissive
  about capturing failure state.
- If no review exists yet for `alpha-release-evidence-cs` when the
  script runs, the bundle records `"review": null` rather than
  fabricating one or omitting the field — visible-missing, not silent.

## Testing

- `scripts/build_release_evidence.py` is evidence-only tooling, like
  `scripts/validate_tasks.py`/`scripts/check_assurance_docs.py` — no
  separate pytest-level unit tests. Verification is running it for real
  and inspecting the resulting JSON, the same posture `packaging-alpha`'s
  `smoke_install.py` took.
- The script's own internal test/smoke invocations (decision 2) ARE the
  required `kind: deployment`-adjacent evidence this task needs — no
  additional test harness is introduced.

## Explicitly out of scope for this phase

- A real GitHub Release (tag, release notes, public announcement) — see
  decision 9. A CI artifact upload satisfies the literal accept criterion
  ("published alongside the alpha artifact") without the much bigger
  step of a public release announcement, which is a separate decision
  for the project owner to make deliberately.
- Actually raising any of `assurance/facts.json`'s `reviewed: false`
  entries to `true` — this task reports the current state honestly, it
  does not do the review work for those capabilities. That's each
  capability's own task, not this one.
- Fixing `extend-scope-atomicity-gap` (filed separately) or any other
  open finding from prior sessions — unrelated to release-evidence
  tooling.
- A GPG-signed or otherwise cryptographically-attested SBOM/bundle —
  `portable-bundle-authenticity` (a separate, not-yet-started Frog-tracked
  task; it has no `tasks/*.weft.yml` entry) already covers
  authenticity/signing; this task's SBOM is a plain digest-and-list,
  matching the rest of this repo's current integrity-without-authenticity
  posture (e.g. `bundle.py`'s own `sha256:` digest with no signature).
