# Packaging alpha — design

Status: approved, 2026-09-05. Design for `packaging-alpha`
(`tasks/70-release.weft.yml`), making WeftMark's install path reproducible
and evidence-backed rather than assumed.

## Why

`packaging-alpha`'s own accept criteria are concrete and currently
unverified: "Core local use installs without proprietary SDK dependencies"
and "Fresh-environment smoke test covers changeset, evidence and review
commands," with required evidence `kind: ci, criterion: Package build and
clean-install smoke test pass on supported Python versions." Nothing in
the repository currently builds a wheel, installs it somewhere clean, or
proves the CLI works outside the dev `.venv`. `packaging-alpha` blocks
`alpha-release-evidence` (P0), which blocks the alpha itself.

## Decisions

1. **`uv build` produces the sdist/wheel; no new project dependency.**
   `uv` is already present in this environment (`/snap/bin/uv`) and builds
   against the existing `[build-system]` (`setuptools>=68`) without
   needing the PyPA `build` package installed. Verified live: `uv build
   --out-dir <dir>` from the repo root produces
   `weftmark-0.0.1-py3-none-any.whl` and a matching sdist with no errors,
   and the wheel is a genuine universal pure-Python wheel (no compiled
   extensions, no platform tag needed).

2. **One script (`scripts/smoke_install.py`) is both the local dev tool
   and the CI evidence step.** It builds the wheel, then for each
   supported Python version creates an isolated `uv venv`, `pip install`s
   the wheel with **no extras**, and drives a real
   `changeset create` → `evidence run` → `review create` cycle against a
   throwaway Git repository — the exact sequence
   `packaging-alpha`'s accept criterion names. A CI-only smoke path that
   diverges from what a maintainer runs locally is exactly the kind of
   drift this project's evidence conventions exist to prevent. Matches
   the existing `scripts/build_*.py` style (stdlib `argparse` +
   `subprocess`, no bespoke shell scripts).

3. **Supported Python versions: 3.11, 3.12, 3.13 — an explicit list, not
   derived from `requires-python` at runtime.** `pyproject.toml` already
   declares `requires-python = ">=3.11"`; this is the enumerable set
   that's actually been verified. Verified live, all three, via `uv venv
   --python <version>` (which fetches an isolated interpreter — 3.11.15
   and 3.13.15 downloaded on demand, 3.12.13 already cached — with no
   system package installation): the full build → install → smoke
   sequence passes on each, `pip install` (not just `uv pip install`)
   also works from the same wheel, and installing on Python 3.10 is
   correctly refused by `requires-python` with a clear pip error rather
   than a confusing failure later.

4. **No new optional-dependency groups.** Every forge adapter
   (`github.py`, `bitbucket.py`, `gitlab.py`, `gitea.py`, `gitea_like.py`,
   `forgejo.py`, `azure_devops.py`), the ACP runtime adapter
   (`adapters/acp.py`), and the HTTP control server (`http/server.py`)
   import stdlib only (`urllib`, `socket`, `subprocess`, `http.server`,
   etc.) — verified by reading every one of those modules' imports this
   session. Only `mcp` and `tui` are real optional extras, and both
   already exist in `pyproject.toml`. `packaging-alpha`'s deliverable
   text ("optional extras for MCP and forge adapters") is satisfied by
   documenting that forge support needs no extra at all, not by adding
   one.

## Package layout

- `scripts/smoke_install.py` — new. Builds via `uv build`; for each
  version in a module-level `SUPPORTED_PYTHON_VERSIONS = ("3.11", "3.12",
  "3.13")` tuple: `uv venv --python <version>`, `uv pip install
  --python <venv>/bin/python <wheel>`, then runs the changeset/evidence/
  review smoke sequence via `subprocess.run([<venv-weftmark-binary>,
  ...])` against a `tempfile.mkdtemp()` Git repo. Exits non-zero on any
  failure, printing which Python version failed.
- `Makefile` — add a `smoke` target calling the script, matching the
  existing `figures`/`html`/`pdf`/`tasks` target pattern.
- `.github/workflows/ci.yml` — one new step in the existing
  `validate-and-build` job, after the existing `Run runtime test suite`
  step: an "Ensure uv is available" guard (matching the existing "Ensure
  Pandoc is available" step's `command -v` check) followed by `make
  smoke`. Not a new job — this keeps it in the same self-hosted runner
  pass as everything else and fails the same way existing steps do.
- `docs/INSTALL.md` — new. WeftMark is not yet published to PyPI (see
  "Explicitly out of scope" below), so the documented install path is
  from a local clone: `pip install .` (core), `pip install .[mcp]`,
  `pip install .[tui]` — or from a built wheel,
  `pip install dist/weftmark-*.whl[mcp]` for anyone using
  `scripts/smoke_install.py`'s own build output. Once actually published,
  this doc's install line becomes `pip install weftmark`; noting that
  explicitly avoids the doc silently overclaiming availability it doesn't
  have yet. Covers the two extras, supported Python versions, and a
  first-commands walkthrough that mirrors exactly what
  `smoke_install.py` exercises (changeset create, evidence run, review
  create) so the doc can never drift from what's actually tested.
- `README.md` — a short new "## Installation" section pointing at
  `docs/INSTALL.md`, placed near the existing "## Repository status"
  section.
- `tasks/70-release.weft.yml` — `packaging-alpha`'s `status: todo` →
  `review` in the same commit as the last implementation slice, per this
  repo's convention (a separate later `plan: close` commit flips it to
  `done`).

## Error handling

- `smoke_install.py` fails loudly and stops at the first failing Python
  version (no "best effort, report at the end" masking) — a packaging
  regression on any supported version is a real regression, not a
  warning.
- The CI guard step for `uv` mirrors the existing Pandoc guard's fail-fast
  posture: if `uv` is missing and cannot be installed, the step fails
  visibly rather than silently skipping the smoke test.

## Testing

- `scripts/smoke_install.py` *is* the evidence for this task's required
  `kind: ci` criterion — running it locally and in CI are the same
  command. No separate pytest-level test is added for it, matching how
  `scripts/validate_tasks.py` and `scripts/check_assurance_docs.py` are
  also evidence-only scripts outside the pytest suite.
- Existing `python -m pytest` suite is unaffected — this task adds no new
  application/domain code, only build tooling and documentation.

## Explicitly out of scope for this phase

- Publishing to PyPI or any package index — this task makes the package
  *installable from a local build*, not *published*. Publication is a
  separate, later decision.
- A CI version matrix (multiple GitHub Actions jobs) — the single
  self-hosted runner already covers all three Python versions inside one
  job via `uv`-managed interpreters; a matrix would only be needed for
  genuinely OS- or platform-specific behavior, which this pure-Python
  package doesn't have.
- Any new optional-dependency group beyond the existing `mcp`/`tui` — no
  adapter in this codebase currently needs one (see decision 4).
- `weftmark-init-doctor` (a separate, not-yet-started source-plan task)
  — onboarding/diagnostics UX is explicitly out of scope for packaging
  itself.
