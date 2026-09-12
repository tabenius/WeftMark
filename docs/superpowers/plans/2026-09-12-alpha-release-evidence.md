# Alpha Release Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce `release/release-evidence-v0.json`, a single, honest,
machine-readable artifact proving exactly what state of the codebase the
first public alpha (`0.1.0a1`) claims to be, and publish it as a CI
artifact alongside the built wheel.

**Architecture:** One new script (`scripts/build_release_evidence.py`)
assembles the bundle by re-running the real test/smoke evidence itself,
reading a native WeftMark review decision from the local ledger, copying
`assurance/facts.json` verbatim, building per-extra SBOMs with `uv`, and
hashing the generated docs. No new application or domain code — this is
evidence-generation tooling plus a version bump and CI wiring.

**Tech Stack:** stdlib (`argparse`, `subprocess`, `json`, `hashlib`,
`tempfile`, `pathlib`), `yaml` (already a `requirements-docs.txt`
dependency), `uv` (already used throughout `packaging-alpha`), WeftMark's
own `LocalGit`/`LedgerService`/`WorkspaceService`/`LocalWorkflowService`
for reading the local ledger.

**Spec:** `docs/superpowers/specs/2026-09-12-alpha-release-evidence-design.md`

## Global Constraints

- The bundle is one file: `release/release-evidence-v0.json`, tagged
  `"schema": "weftmark.release-evidence.v0"`.
- `release/` is gitignored — the bundle embeds the exact commit SHA it
  was built from and must never be committed (it would immediately go
  stale).
- The script re-runs `python -m pytest -q` and `make smoke` itself at
  generation time — no GitHub Actions API dependency, no token.
- The review decision comes from a real native WeftMark review on Change
  Set `alpha-release-evidence-cs`, read via
  `LocalWorkflowService.list_reviews(change_set_id="alpha-release-evidence-cs")`
  — the same ledger-reading pattern `BundleService.export()` already uses.
  If no review exists yet, the bundle records `"review": null`, never a
  fabricated or omitted value.
- `assurance/facts.json` is embedded verbatim, including any
  `"reviewed": false` entries — never filtered or summarized away.
- SBOM: for each of the two real extras (`mcp`, `tui`), a fresh `uv venv`
  + install + `uv pip list --format json` — the base install's SBOM is an
  empty list.
- Docs digest: SHA-256 of `build/weftmark.html`, `build/weftmark_A5.pdf`
  (after a real `make docs` run), and `docs/weftmark.mdx`.
- Version bumps to `0.1.0a1` in **two** places that don't reference each
  other (a pre-existing, unrelated drift risk, not something to
  architecturally fix here): `pyproject.toml`'s `version` field and
  `src/weftmark/__init__.py`'s `__version__` string. A test in
  `tests/architecture/test_domain_dependencies.py` hardcodes the old
  value and must be updated in the same commit.
- Publish via CI artifact upload (`actions/upload-artifact`) alongside
  `dist/*.whl` — never a public GitHub Release.
- Match this repo's existing `scripts/*.py` style: `#!/usr/bin/env
  python3` shebang, module docstring, `from __future__ import
  annotations`, `ROOT = Path(__file__).resolve().parents[1]`, a custom
  exception class for expected failures, `def main() -> int`, `raise
  SystemExit(main())` at the bottom.

---

### Task 1: `.gitignore` — exclude `release/`

**Files:**
- Modify: `.gitignore`

**Interfaces:**
- Produces: `release/` excluded from version control, matching the
  existing `dist/` entry's style and placement (added during
  `packaging-alpha`).

- [ ] **Step 1: Add the entry**

In `.gitignore`, after the existing block:
```
# Package build artifacts (sdist/wheel from `uv build` / scripts/smoke_install.py)
dist/
```
add:
```

# Release evidence bundle (embeds the exact commit SHA it was built from;
# never committed — see scripts/build_release_evidence.py)
release/
```

- [ ] **Step 2: Verify it's ignored**

Run: `mkdir -p release && echo '{}' > release/release-evidence-v0.json && git status --porcelain`
Expected: `release/release-evidence-v0.json` does NOT appear in the output. Then: `rm -rf release`.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore the release evidence bundle (release/)"
```

---

### Task 2: Version bump to `0.1.0a1`

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/weftmark/__init__.py`
- Modify: `tests/architecture/test_domain_dependencies.py:60`

**Interfaces:**
- Produces: `weftmark.__version__ == "0.1.0a1"`, `pyproject.toml`'s
  `[project].version == "0.1.0a1"`. Task 3's script reads
  `pyproject.toml`'s version field directly (by parsing the TOML, not by
  importing the package) to embed in the bundle.

- [ ] **Step 1: Bump both version strings**

In `pyproject.toml`, change:
```toml
version = "0.0.1"
```
to:
```toml
version = "0.1.0a1"
```

In `src/weftmark/__init__.py`, change:
```python
__version__ = "0.0.1"
```
to:
```python
__version__ = "0.1.0a1"
```

- [ ] **Step 2: Update the hardcoded test assertion**

In `tests/architecture/test_domain_dependencies.py`, find:
```python
    assert weftmark.__version__ == "0.0.1"
```
change to:
```python
    assert weftmark.__version__ == "0.1.0a1"
```

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass (same count as before this task — this is a
version-string change only).

- [ ] **Step 4: Verify the built wheel picks up the new version**

Run: `uv build && ls dist/`
Expected: `dist/weftmark-0.1.0a1-py3-none-any.whl` and
`dist/weftmark-0.1.0a1.tar.gz` (version string in the filename changed).
Then clean up: `rm -rf dist build src/weftmark.egg-info`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/weftmark/__init__.py tests/architecture/test_domain_dependencies.py
git commit -m "chore: bump version to 0.1.0a1 for the first alpha"
```

---

### Task 3: `scripts/build_release_evidence.py`

**Files:**
- Create: `scripts/build_release_evidence.py`

**Interfaces:**
- Produces: `python3 scripts/build_release_evidence.py [--repo PATH]`
  writing `release/release-evidence-v0.json`. The bundle is always
  written if generation reaches that point — the *file* records a
  test/smoke failure rather than omitting it (Global Constraints) — but
  the *process* still exits non-zero whenever the embedded pytest/`make
  smoke` evidence shows a failure, or generation itself couldn't
  complete (can't write the file, can't read the ledger, `uv
  build`/`make docs` failed). CI must fail loudly on bad evidence; only
  the bundle's own contents are permissive about capturing failure
  state, matching the spec's Error Handling section.
- Consumes: Task 2's version bump (reads `pyproject.toml`'s version
  field directly).

This task has no pytest-level tests — like `scripts/smoke_install.py` and
`scripts/validate_tasks.py`, this is evidence-generation tooling whose
verification is running it for real and inspecting the output (Steps 3-5
below).

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Assemble the first alpha's release evidence bundle.

Composes version + source SHA, a task-graph snapshot, self-run
test/smoke evidence, the native release-readiness review decision, the
assurance-facts rollup, a per-extra SBOM, and a generated-docs digest
into one schema-versioned JSON file: release/release-evidence-v0.json.

Re-runs pytest and `make smoke` itself rather than querying external CI
state, so this script produces the same result locally and in CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from weftmark.adapters.git_local import LocalGit
from weftmark.adapters.jsonl_ledger import JsonlLedger
from weftmark.application.change_binding import ChangeBindingError
from weftmark.application.ledger import LedgerService
from weftmark.application.local_workflow import LocalWorkflowService
from weftmark.application.workspace import WorkspaceService
from weftmark.domain.evidence import EvidenceProducer, ProducerKind

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "release"
BUNDLE_PATH = RELEASE_DIR / "release-evidence-v0.json"
SCHEMA = "weftmark.release-evidence.v0"
RELEASE_CHANGE_SET_ID = "alpha-release-evidence-cs"
SBOM_EXTRAS = ("mcp", "tui")
OUTPUT_TAIL_LINES = 40


class ReleaseEvidenceError(RuntimeError):
    """Raised when the bundle itself cannot be assembled or written."""


def _run(command: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = (result.stdout or "") + (result.stderr or "")
    tail = "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
    return {
        "command": command,
        "returncode": result.returncode,
        "output_tail": tail,
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _source_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _task_graph_snapshot() -> dict[str, Any]:
    import yaml

    by_status: dict[str, int] = {}
    total = 0
    files: list[str] = []
    for path in sorted((ROOT / "tasks").glob("*.weft.yml")):
        files.append(str(path.relative_to(ROOT)))
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for task in data.get("tasks", []):
            status = str(task.get("status", "unknown"))
            by_status[status] = by_status.get(status, 0) + 1
            total += 1
    return {"total": total, "by_status": by_status, "files": files}


def _test_evidence() -> dict[str, Any]:
    return {
        "pytest": _run([sys.executable, "-m", "pytest", "-q"]),
        "make_smoke": _run(["make", "smoke"]),
    }


def _release_review(repo: str) -> dict[str, Any] | None:
    git = LocalGit(repo)
    repository = git.repository()
    if not repository.id.startswith("git:"):
        raise ChangeBindingError("local repository identity cannot select a ledger")
    ledger_path = Path(repository.id.removeprefix("git:")) / "weftmark" / "ledger.jsonl"
    ledger = LedgerService(JsonlLedger(ledger_path))
    workspace = WorkspaceService(git, ledger)
    workflow = LocalWorkflowService(
        workspace, ledger, EvidenceProducer(ProducerKind.WORKER, "weftmark-release-evidence")
    )
    reviews = workflow.list_reviews(change_set_id=RELEASE_CHANGE_SET_ID)
    if not reviews:
        return None
    latest = reviews[-1]
    decision = latest["decision"]
    return {
        "change_set_id": RELEASE_CHANGE_SET_ID,
        "review_id": decision["id"],
        "outcome": decision["outcome"],
        "head_sha": decision["head_sha"],
        "is_releasable": latest["is_releasable"],
    }


def _assurance_facts() -> dict[str, Any]:
    return json.loads((ROOT / "assurance" / "facts.json").read_text(encoding="utf-8"))


def _sbom() -> dict[str, Any]:
    import tempfile

    extras: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="weftmark-release-sbom-") as tmp:
        work_root = Path(tmp)
        wheel_result = subprocess.run(
            ["uv", "build", "--out-dir", str(work_root / "dist")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if wheel_result.returncode != 0:
            raise ReleaseEvidenceError(
                f"uv build failed for SBOM generation: {wheel_result.stderr}"
            )
        wheels = sorted((work_root / "dist").glob("*.whl"))
        if not wheels:
            raise ReleaseEvidenceError("uv build produced no wheel for SBOM generation")
        wheel = wheels[0]

        for extra in SBOM_EXTRAS:
            venv_dir = work_root / f"venv-{extra}"
            subprocess.run(
                ["uv", "venv", "--python", "3.13", str(venv_dir)],
                capture_output=True,
                text=True,
                check=True,
                timeout=300,
            )
            python_bin = venv_dir / "bin" / "python"
            subprocess.run(
                [
                    "uv", "pip", "install", "--python", str(python_bin),
                    f"{wheel}[{extra}]",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=300,
            )
            list_result = subprocess.run(
                ["uv", "pip", "list", "--python", str(python_bin), "--format", "json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
            extras[extra] = json.loads(list_result.stdout)
    return {"base": [], "extras": extras}


def _docs_digest() -> dict[str, Any]:
    result = subprocess.run(
        ["make", "docs"], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise ReleaseEvidenceError(f"make docs failed: {result.stderr}")
    return {
        "source": {
            "path": "docs/weftmark.mdx",
            "sha256": _sha256_file(ROOT / "docs" / "weftmark.mdx"),
        },
        "artifacts": [
            {
                "path": "build/weftmark.html",
                "sha256": _sha256_file(ROOT / "build" / "weftmark.html"),
            },
            {
                "path": "build/weftmark_A5.pdf",
                "sha256": _sha256_file(ROOT / "build" / "weftmark_A5.pdf"),
            },
        ],
    }


def build_bundle(repo: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": _read_version(),
        "source_sha": _source_sha(),
        "task_graph": _task_graph_snapshot(),
        "test_evidence": _test_evidence(),
        "review": _release_review(repo),
        "assurance_facts": _assurance_facts(),
        "sbom": _sbom(),
        "docs_digest": _docs_digest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="path inside the Git repository")
    args = parser.parse_args(argv)

    try:
        bundle = build_bundle(args.repo)
        RELEASE_DIR.mkdir(parents=True, exist_ok=True)
        BUNDLE_PATH.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except ReleaseEvidenceError as error:
        print(f"build_release_evidence: {error}", file=sys.stderr)
        return 1

    print(f"wrote {BUNDLE_PATH.relative_to(ROOT)}")
    pytest_ok = bundle["test_evidence"]["pytest"]["returncode"] == 0
    smoke_ok = bundle["test_evidence"]["make_smoke"]["returncode"] == 0
    if not pytest_ok or not smoke_ok:
        print(
            "build_release_evidence: bundle written, but embedded evidence "
            f"shows a failure (pytest ok={pytest_ok}, make smoke ok={smoke_ok})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it for real**

Run: `python3 scripts/build_release_evidence.py`
Expected: prints `wrote release/release-evidence-v0.json`, exit code 0.
This will take a few minutes (it builds the docs, runs the full pytest
suite, runs `make smoke`, and builds two extra SBOM venvs) — that's
expected, this is a real, self-verifying evidence run, not a fast smoke
check.

- [ ] **Step 3: Inspect the bundle**

Run: `python3 -m json.tool release/release-evidence-v0.json | head -60`
Expected: valid JSON, `"schema": "weftmark.release-evidence.v0"`,
`"version": "0.1.0a1"`, a real 40-character `source_sha`, a `task_graph`
with `"total": 82` (or higher, if more tasks have landed since this plan
was written) and a `by_status` breakdown, and `"review": null` (no
release-readiness review exists yet — this is Task 6's job, expected at
this point).

- [ ] **Step 4: Verify the SBOM and docs digest sections are populated**

Run: `python3 -c "
import json
data = json.load(open('release/release-evidence-v0.json'))
assert data['sbom']['base'] == []
assert len(data['sbom']['extras']['mcp']) > 0
assert len(data['sbom']['extras']['tui']) > 0
assert len(data['docs_digest']['source']['sha256']) == 64
assert len(data['docs_digest']['artifacts']) == 2
print('sbom and docs_digest OK')
"`
Expected: `sbom and docs_digest OK`.

- [ ] **Step 5: Clean up and commit**

```bash
rm -rf release dist build src/weftmark.egg-info
git add scripts/build_release_evidence.py
git commit -m "feat: add scripts/build_release_evidence.py"
```

---

### Task 4: `Makefile` — `release-evidence` target

**Files:**
- Modify: `Makefile`

**Interfaces:**
- Consumes: `scripts/build_release_evidence.py` (Task 3).
- Produces: `make release-evidence`, matching the existing
  `smoke`/`tasks`/`figures` target pattern.

- [ ] **Step 1: Add the target**

In `Makefile`, change:
```makefile
.PHONY: all docs html pdf figures logo tasks rev0 smoke clean

all: docs logo tasks
```
to:
```makefile
.PHONY: all docs html pdf figures logo tasks rev0 smoke release-evidence clean

all: docs logo tasks
```

Then, after the existing `smoke:` target:
```makefile
smoke:
	$(PYTHON) scripts/smoke_install.py
```
add:
```makefile

release-evidence:
	$(PYTHON) scripts/build_release_evidence.py
```

- [ ] **Step 2: Verify it runs**

Run: `make release-evidence`
Expected: same output/exit code as Task 3 Step 2. Then clean up:
`rm -rf release dist build src/weftmark.egg-info`.

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "build: add make release-evidence target"
```

---

### Task 5: CI wiring — build and upload the release evidence bundle

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `make release-evidence` (Task 4).

- [ ] **Step 1: Add the step after the packaging smoke test**

In `.github/workflows/ci.yml`, immediately after the existing:
```yaml
      - name: Package build and clean-install smoke test
        run: make smoke
```
add:
```yaml

      - name: Build the release evidence bundle
        run: make release-evidence

      - name: Upload release artifacts
        uses: actions/upload-artifact@v7
        with:
          name: weftmark-release-evidence
          path: |
            dist/*.whl
            release/release-evidence-v0.json
          if-no-files-found: error
```

- [ ] **Step 2: Validate the YAML is well-formed**

Run: `.venv/bin/python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" && echo "valid YAML"`
Expected: `valid YAML`, no exception.

- [ ] **Step 3: Verify the actions/upload-artifact version matches this repo's existing usage**

Run: `grep -n "actions/upload-artifact" .github/workflows/ci.yml`
Expected: two matches, both `actions/upload-artifact@v7` — the
pre-existing "Upload documentation build" step and this new step must
use the identical pinned version.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: build and upload the release evidence bundle alongside the wheel"
```

---

### Task 6: Close out the source-plan task

**Files:**
- Modify: `tasks/70-release.weft.yml`

**Interfaces:**
- Consumes: `scripts/build_release_evidence.py` (Task 3).

This task does **not** create the release-readiness review or the
`kind: deployment` evidence the source task actually requires
(`tasks/70-release.weft.yml`'s own `evidence:` entry is `kind:
deployment, criterion: Release bundle is published alongside the alpha
artifact` — that can only be honestly satisfied once a real CI run has
actually built and uploaded the artifacts, which only happens after
these commits are pushed; see "After this plan" below). This task only
confirms Tasks 1-5 compose correctly and flips the source-plan status to
`review`, matching this repo's convention that an implementation commit
sets status to `review`, not `done`.

- [ ] **Step 1: Run the full evidence bundle once more, now that Tasks 1-5 are committed together**

Run: `python3 scripts/build_release_evidence.py`
Expected: exit code 0, `release/release-evidence-v0.json` written,
`"review": null` (expected and correct — no release-readiness review
exists yet; that's exactly what "After this plan" covers).

- [ ] **Step 2: Flip the source-plan task status**

In `tasks/70-release.weft.yml`, change the `alpha-release-evidence`
entry's:
```yaml
    status: todo
```
to:
```yaml
    status: review
```

- [ ] **Step 3: Validate the task graph and run the full suite one last time**

Run: `.venv/bin/python scripts/validate_tasks.py`
Expected: validates cleanly.

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass (same count as after Task 2 — no new
application code was added by this plan beyond the version-string
change).

- [ ] **Step 4: Clean up and commit**

```bash
rm -rf release dist build src/weftmark.egg-info
git add tasks/70-release.weft.yml
git commit -m "chore: mark alpha-release-evidence review"
```

---

## After this plan

This plan produces working, self-verifying release-evidence tooling, but
does **not** cover completing the native WeftMark/Frog claim-and-evidence
bookkeeping this repo's `AGENTS.md` requires around the task itself.
That's session-level process, matching the sequence already used for
`terminal-review-ui` and `packaging-alpha` (see git history on `main`),
and specifically requires — in this order:

1. Push these commits and verify a real CI run on the exact pushed
   commit actually builds and uploads both `dist/*.whl` and
   `release/release-evidence-v0.json` (Task 5's new steps) — the same
   "don't infer from local runs alone" verification `packaging-alpha`'s
   closeout did for its own `kind: ci` evidence.
2. Only then record the source task's actual required evidence —
   `weftmark evidence run alpha-release-evidence-cs --kind deployment
   --command <something that names the verified CI run>` — and create
   the release-readiness review with `weftmark review create
   alpha-release-evidence-cs --author claude --require deployment`. If
   the outcome is anything other than `ready`, do not proceed to close
   the task; that would fabricate release readiness.
3. Re-run `scripts/build_release_evidence.py` once more so the bundle's
   `"review"` field picks up that real review (this is the *only* point
   in the whole sequence where the bundle's review section is genuinely
   populated — never fabricate it earlier).
4. Transition the Change Set to `merged`, complete the native task,
   finish the Frog task, and make the separate `plan: close` commit
   flipping `tasks/70-release.weft.yml`'s status from `review` to
   `done`.
