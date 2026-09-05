# Packaging Alpha Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make WeftMark's local install path reproducible and evidence-backed:
a built wheel, a smoke test that installs it fresh (no extras) and exercises
changeset/evidence/review across Python 3.11/3.12/3.13, CI wiring, and
install documentation.

**Architecture:** One new script (`scripts/smoke_install.py`) is both the
local dev tool and the CI evidence step — it builds the wheel with `uv
build`, then for each supported Python version creates an isolated `uv
venv`, installs the wheel with no extras, and drives a real
changeset/evidence/review cycle against a throwaway Git repo. No new
application or domain code; this is packaging/build tooling plus docs.

**Tech Stack:** `uv` (build backend driver + isolated interpreter/venv
management — already present in this environment, no new project
dependency), stdlib `argparse`/`subprocess`/`tempfile`, existing
`setuptools` build backend (unchanged).

**Spec:** `docs/superpowers/specs/2026-09-05-packaging-alpha-design.md`

## Global Constraints

- No new entries in `[project.dependencies]` or a new optional-dependency
  group — every forge adapter, the ACP runtime adapter, and the HTTP
  control server are stdlib-only; only `mcp` and `tui` are real extras and
  both already exist.
- Supported Python versions for the smoke test are exactly `3.11`, `3.12`,
  `3.13` (a literal tuple, not derived from `requires-python` at runtime).
- The smoke test installs the wheel with **no extras** (matches the accept
  criterion "Core local use installs without proprietary SDK
  dependencies").
- `scripts/smoke_install.py` must fail fast: stop and exit non-zero at the
  first failing Python version, not collect failures and report at the
  end.
- WeftMark is not published to PyPI yet — `docs/INSTALL.md` must document
  installing from a local clone or a locally built wheel, never
  `pip install weftmark` unqualified.
- Match this repo's existing `scripts/*.py` style exactly: `#!/usr/bin/env
  python3` shebang, module docstring, `from __future__ import
  annotations`, `ROOT = Path(__file__).resolve().parents[1]`, a custom
  exception class for expected failures, `def main() -> int`, `raise
  SystemExit(main())` at the bottom (see `scripts/check_assurance_docs.py`
  for the exact pattern).

---

### Task 1: `dist/` gitignore entry

**Files:**
- Modify: `.gitignore`

**Interfaces:**
- Produces: `dist/` excluded from version control, matching the existing
  `build/*` section's style and placement.

- [ ] **Step 1: Add the gitignore entry**

In `.gitignore`, after the existing block:
```
# Generated documentation builds (rev0 artifacts under docs/artifacts are committed)
build/*
!build/.gitkeep
```
add:
```

# Package build artifacts (sdist/wheel from `uv build` / scripts/smoke_install.py)
dist/
```

- [ ] **Step 2: Verify a build no longer shows as untracked**

Run: `uv build && git status --porcelain`
Expected: `dist/weftmark-0.0.1.tar.gz` and `dist/weftmark-0.0.1-py3-none-any.whl` exist on disk but do NOT appear in `git status --porcelain` output. Then clean up: `rm -rf dist build src/weftmark.egg-info`.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore package build artifacts (dist/)"
```

---

### Task 2: `scripts/smoke_install.py`

**Files:**
- Create: `scripts/smoke_install.py`

**Interfaces:**
- Produces: an executable script, `python3 scripts/smoke_install.py` (or `scripts/smoke_install.py` directly, matching other scripts in this repo which are chmod +x — verify with `ls -l scripts/*.py` and match). Exit code 0 on full success, 1 on any failure, with a clear message naming which Python version and which step failed.
- Consumes: `uv` on `PATH` (build + venv + pip-install), the `weftmark` console script installed into each per-version venv.

This task has no pytest-level tests — per the spec, this script *is* the
evidence for `packaging-alpha`'s required `kind: ci` criterion, matching
how `scripts/validate_tasks.py` and `scripts/check_assurance_docs.py` are
also evidence-only scripts outside the pytest suite. Verification is
running the script itself successfully.

- [ ] **Step 1: Check existing scripts' executable bit for style match**

Run: `ls -l scripts/*.py`
Expected: note whether they're `+x` (executable) or plain `-rw-r--r--`; match that mode for the new file in Step 3.

- [ ] **Step 2: Write the script**

```python
#!/usr/bin/env python3
"""Build the WeftMark wheel and smoke-test a clean, no-extras install.

Builds the sdist/wheel with `uv build`, then for each supported Python
version creates an isolated venv, installs the wheel with no optional
extras, and drives a real changeset/evidence/review cycle against a
throwaway Git repository. This is packaging-alpha's required clean-install
evidence — the same command runs locally and in CI, so the documented
install path can never silently drift from what's actually tested.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist"
SUPPORTED_PYTHON_VERSIONS = ("3.11", "3.12", "3.13")


class SmokeInstallError(RuntimeError):
    """Raised when a build, install, or smoke-command step fails."""


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(
        command,
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SmokeInstallError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


def build_wheel() -> Path:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    _run(["uv", "build"])
    wheels = sorted(DIST_DIR.glob("*.whl"))
    if not wheels:
        raise SmokeInstallError(f"uv build produced no wheel in {DIST_DIR}")
    return wheels[0]


def smoke_one_version(version: str, wheel: Path, work_root: Path) -> None:
    venv_dir = work_root / f"venv-{version}"
    _run(["uv", "venv", "--python", version, str(venv_dir)])
    python_bin = venv_dir / "bin" / "python"
    weftmark_bin = venv_dir / "bin" / "weftmark"

    _run(["uv", "pip", "install", "--python", str(python_bin), str(wheel)])

    repo_dir = work_root / f"repo-{version}"
    repo_dir.mkdir()
    _run(["git", "init", "--quiet", "--initial-branch=main"], cwd=repo_dir)
    _run(["git", "config", "user.name", "smoke-install"], cwd=repo_dir)
    _run(["git", "config", "user.email", "smoke-install@example.invalid"], cwd=repo_dir)
    _run(["git", "commit", "--quiet", "--allow-empty", "-m", "base"], cwd=repo_dir)

    _run(
        [
            str(weftmark_bin),
            "--repo",
            str(repo_dir),
            "changeset",
            "create",
            "smoke-cs",
            "--goal",
            "packaging smoke test",
            "--scope",
            "file:**",
        ]
    )
    _run(
        [
            str(weftmark_bin),
            "--repo",
            str(repo_dir),
            "evidence",
            "run",
            "smoke-cs",
            "--kind",
            "test",
            "--command",
            "echo",
            "ok",
        ]
    )
    _run(
        [
            str(weftmark_bin),
            "--repo",
            str(repo_dir),
            "review",
            "create",
            "smoke-cs",
            "--author",
            "smoke-install",
            "--require",
            "test",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    try:
        wheel = build_wheel()
        print(f"built {wheel.relative_to(ROOT)}")
        with tempfile.TemporaryDirectory(prefix="weftmark-smoke-") as tmp:
            work_root = Path(tmp)
            for version in SUPPORTED_PYTHON_VERSIONS:
                print(f"smoke-testing Python {version} ...")
                smoke_one_version(version, wheel, work_root)
                print(f"Python {version}: ok")
    except SmokeInstallError as error:
        print(f"smoke_install: {error}", file=sys.stderr)
        return 1

    print(
        f"smoke_install: {len(SUPPORTED_PYTHON_VERSIONS)} Python versions "
        "passed changeset/evidence/review against a clean, no-extras install"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Set the executable bit to match the repo's convention**

If Step 1 found the existing scripts are `+x`, run: `chmod +x scripts/smoke_install.py`. If they're plain files, skip this step.

- [ ] **Step 4: Run it for real**

Run: `python3 scripts/smoke_install.py`
Expected: prints `built dist/weftmark-0.0.1-py3-none-any.whl`, then `smoke-testing Python 3.11 ...` / `Python 3.11: ok` for each of the three versions in order, then the final `smoke_install: 3 Python versions passed ...` line, exit code 0. This should take well under a minute (the individual steps were verified taking a few seconds each in the design's spike).

- [ ] **Step 5: Verify the failure path is real, not just the happy path**

Run: `python3 -c "
import sys
sys.path.insert(0, 'scripts')
from smoke_install import SmokeInstallError, _run
try:
    _run(['false'])
except SmokeInstallError as e:
    print('caught:', str(e)[:50])
    sys.exit(0)
sys.exit(1)
"`
Expected: prints `caught: command failed (1): false` and exits 0 — confirms `_run` actually raises on a failing subprocess rather than silently succeeding.

- [ ] **Step 6: Clean up build artifacts before committing**

Run: `rm -rf dist build src/weftmark.egg-info && git status --porcelain`
Expected: clean (no untracked build artifacts) — Task 1's `.gitignore` entry should already prevent `dist/` from showing, but `build/` and `*.egg-info/` are also gitignored already; this step just confirms no stray files leak into the commit.

- [ ] **Step 7: Commit**

```bash
git add scripts/smoke_install.py
git commit -m "feat: add scripts/smoke_install.py as packaging-alpha's clean-install evidence"
```

---

### Task 3: Makefile `smoke` target

**Files:**
- Modify: `Makefile`

**Interfaces:**
- Consumes: `scripts/smoke_install.py` (Task 2).
- Produces: `make smoke` as a documented, discoverable entry point matching the existing `figures`/`html`/`pdf`/`tasks` targets.

- [ ] **Step 1: Add the target**

In `Makefile`, change:
```makefile
.PHONY: all docs html pdf figures logo tasks rev0 clean

all: docs logo tasks
```
to:
```makefile
.PHONY: all docs html pdf figures logo tasks rev0 smoke clean

all: docs logo tasks
```

Then, after the existing `tasks:` target:
```makefile
tasks:
	$(PYTHON) scripts/validate_tasks.py
```
add:
```makefile

smoke:
	$(PYTHON) scripts/smoke_install.py
```

- [ ] **Step 2: Verify it runs**

Run: `make smoke`
Expected: same output and exit code as running the script directly in Task 2 Step 4.

- [ ] **Step 3: Clean up and commit**

```bash
rm -rf dist build src/weftmark.egg-info
git add Makefile
git commit -m "build: add make smoke target for the packaging clean-install evidence"
```

---

### Task 4: CI wiring

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `make smoke` (Task 3).

- [ ] **Step 1: Add an "Ensure uv is available" guard step**

In `.github/workflows/ci.yml`, immediately after the existing:
```yaml
      - name: Ensure Pandoc is available
        run: |
          if ! command -v pandoc >/dev/null 2>&1; then
            sudo apt-get update && sudo apt-get install -y pandoc
          else
            echo "pandoc already installed: $(pandoc --version | head -1)"
          fi
```
add:
```yaml

      - name: Ensure uv is available
        run: |
          if ! command -v uv >/dev/null 2>&1; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
            echo "$HOME/.local/bin" >> "$GITHUB_PATH"
          else
            echo "uv already installed: $(uv --version)"
          fi
```

- [ ] **Step 2: Add the smoke-test step after the existing test-suite step**

Immediately after the existing:
```yaml
      - name: Run runtime test suite
        run: python -m pytest
```
add:
```yaml

      - name: Package build and clean-install smoke test
        run: make smoke
```

- [ ] **Step 3: Validate the YAML is well-formed**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" && echo "valid YAML"`
Expected: `valid YAML`, no exception. (If `PyYAML` isn't in the dev `.venv`, run this with the system `python3` instead — `requirements-docs.txt` already pins `PyYAML>=6,<7`, so `.venv`'s `python` should have it.)

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run the packaging clean-install smoke test in validate-and-build"
```

---

### Task 5: `docs/INSTALL.md`

**Files:**
- Create: `docs/INSTALL.md`

**Interfaces:**
- Consumes: nothing (pure documentation), but its commands must match exactly what `scripts/smoke_install.py` (Task 2) actually runs, so the doc can never drift from what's tested.

- [ ] **Step 1: Write the file**

```markdown
# Installing WeftMark

WeftMark is not yet published to PyPI. Install it from a local clone —
either directly (editable or not) or from a wheel you build yourself.

## Requirements

- Python 3.11, 3.12, or 3.13.
- Git (WeftMark reads and observes your repository's Git history; it never
  requires network access to function).

## Core install (from a clone)

```bash
git clone https://github.com/tabenius/WeftMark.git
cd WeftMark
pip install .
```

This installs the `weftmark` CLI with **no extra dependencies** — every
forge adapter (GitHub, GitLab, Bitbucket, Gitea, Forgejo, Azure DevOps),
the ACP runtime adapter, and the local HTTP control surface use only the
Python standard library. Nothing here depends on a model-provider SDK.

## Optional extras

Two capabilities are opt-in because they pull in real third-party
dependencies:

```bash
pip install '.[mcp]'   # the weftmark-mcp server (Model Context Protocol)
pip install '.[tui]'   # weftmark tui, the terminal reviewer (Textual)
```

Both can be installed together: `pip install '.[mcp,tui]'`.

## Installing from a built wheel

If you'd rather build a wheel first (for example, to install it somewhere
without a full clone):

```bash
uv build          # or: python -m build
pip install dist/weftmark-*.whl
```

## Verifying your install

```bash
weftmark --help
```

should print the top-level command list (`status`, `tui`, `bundle`,
`task`, `changeset`, `claim`, `scope`, `evidence`, `review`, `handoff`,
...).

A minimal end-to-end check, run inside any Git repository:

```bash
weftmark changeset create smoke-cs --goal "first change set" --scope "file:**"
weftmark evidence run smoke-cs --kind test --command echo ok
weftmark review create smoke-cs --author "$(whoami)" --require test
```

The last command should print a `ready` outcome. This is exactly the
sequence `scripts/smoke_install.py` runs automatically against a fresh,
no-extras install on every supported Python version as part of this
project's own CI.
```

- [ ] **Step 2: Verify the doc's commands actually work as written**

Run these exact commands (from a scratch directory, using the repo's built wheel):
```bash
cd /data/src/experiments/WeftMark
uv build
D=$(mktemp -d)
python3 -m venv "$D/venv"
"$D/venv/bin/pip" install --quiet dist/weftmark-*.whl
"$D/venv/bin/weftmark" --help | head -3
cd "$D"
git init --quiet --initial-branch=main
git config user.name smoke
git config user.email smoke@example.invalid
git commit --quiet --allow-empty -m base
"$D/venv/bin/weftmark" changeset create smoke-cs --goal "first change set" --scope "file:**"
"$D/venv/bin/weftmark" evidence run smoke-cs --kind test --command echo ok
"$D/venv/bin/weftmark" review create smoke-cs --author "$(whoami)" --require test
cd /data/src/experiments/WeftMark
rm -rf "$D" dist build src/weftmark.egg-info
```
Expected: every command succeeds; the final `review create` line's output starts with a review ID followed by `ready`.

- [ ] **Step 3: Commit**

```bash
git add docs/INSTALL.md
git commit -m "docs: add docs/INSTALL.md"
```

---

### Task 6: README install section

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: `docs/INSTALL.md` (Task 5).

- [ ] **Step 1: Insert the section**

In `README.md`, find the end of the assurance snapshot table — the line:
```
<!-- assurance:end -->
```
immediately followed by a blank line and then:
```
## From Frog to WeftMark
```

Insert a new section between them, so it reads:
```
<!-- assurance:end -->

## Installation

WeftMark is not yet published to PyPI; install it from a local clone.
See [`docs/INSTALL.md`](docs/INSTALL.md) for the full guide (supported
Python versions, optional extras, and a verification walkthrough). The
short version:

```bash
git clone https://github.com/tabenius/WeftMark.git
cd WeftMark
pip install .
```

## From Frog to WeftMark
```

- [ ] **Step 2: Verify `scripts/check_assurance_docs.py` still passes**

Run: `.venv/bin/python scripts/check_assurance_docs.py`
Expected: `assurance check: N facts and M claims valid`, exit code 0 — confirms the new section didn't disturb the `<!-- assurance:begin -->`/`<!-- assurance:end -->` block the script parses.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add a short Installation section to README, linking docs/INSTALL.md"
```

---

### Task 7: Close out the source-plan task

**Files:**
- Modify: `tasks/70-release.weft.yml`

**Interfaces:**
- Consumes: nothing new.

- [ ] **Step 1: Flip the status**

In `tasks/70-release.weft.yml`, change the `packaging-alpha` entry's:
```yaml
    status: todo
```
to:
```yaml
    status: review
```

- [ ] **Step 2: Validate the task graph**

Run: `.venv/bin/python scripts/validate_tasks.py`
Expected: validates cleanly, no schema/dependency errors.

- [ ] **Step 3: Run the full test suite one last time**

Run: `.venv/bin/python -m pytest -q`
Expected: all existing tests pass — this plan added no application/domain code, so the count should be unchanged from before Task 1.

- [ ] **Step 4: Commit**

```bash
git add tasks/70-release.weft.yml
git commit -m "test: add packaging clean-install smoke evidence, mark packaging-alpha review"
```

---

## After this plan

This plan produces working, merge-ready packaging tooling but does **not**
cover the native WeftMark/Frog claim-and-evidence bookkeeping this repo's
`AGENTS.md` requires around it (running `weftmark evidence run` against the
native Change Set, `weftmark review create`, transitioning to `merged`,
completing the native task, finishing the Frog task, and the separate
`plan: close` commit flipping `tasks/70-release.weft.yml`'s status from
`review` to `done`). That's session-level process, not an implementation
task, and should follow the same sequence already used for
`terminal-review-ui` and `task-claim-scope-amendment-recovery` (see git
history on `main`).
