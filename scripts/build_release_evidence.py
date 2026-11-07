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
import tempfile
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
COMMAND_TIMEOUT_SECONDS = 1200


class ReleaseEvidenceError(RuntimeError):
    """Raised when the bundle itself cannot be assembled or written."""


def _tail(output: str) -> str:
    return "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])


def _run(command: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd or ROOT,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {
            "command": command,
            "returncode": 127,
            "output_tail": f"command not found: {command[0]}",
        }
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or "") + (error.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {
            "command": command,
            "returncode": 124,
            "output_tail": _tail(
                output
                + f"\ntimed out after {COMMAND_TIMEOUT_SECONDS}s: {' '.join(command)}"
            ),
        }
    output = (result.stdout or "") + (result.stderr or "")
    return {
        "command": command,
        "returncode": result.returncode,
        "output_tail": _tail(output),
    }


def _checked(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    context: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd or ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError) as error:
        raise ReleaseEvidenceError(
            f"{context}: command not found: {command[0]}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ReleaseEvidenceError(
            f"{context}: timed out after {timeout}s: {' '.join(command)}"
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or "") + (result.stdout or "")
        raise ReleaseEvidenceError(
            f"{context}: failed ({result.returncode}): {_tail(detail)}"
        )
    return result


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _source_sha() -> str:
    result = _checked(
        ["git", "rev-parse", "HEAD"], context="source SHA resolution"
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
    common_dir = Path(repository.id.removeprefix("git:"))
    ledger_path = common_dir / "weftmark" / "ledger.jsonl"
    ledger = LedgerService(JsonlLedger(ledger_path))
    workspace = WorkspaceService(git, ledger)
    workflow = LocalWorkflowService(
        workspace,
        ledger,
        EvidenceProducer(ProducerKind.WORKER, "weftmark-release-evidence"),
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
    extras: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="weftmark-release-sbom-") as tmp:
        work_root = Path(tmp)
        dist_dir = work_root / "dist"
        _checked(
            ["uv", "build", "--out-dir", str(dist_dir)],
            context="uv build for SBOM generation",
        )
        wheels = sorted(dist_dir.glob("*.whl"))
        if not wheels:
            raise ReleaseEvidenceError("uv build produced no wheel for SBOM generation")
        wheel = wheels[0]

        for extra in SBOM_EXTRAS:
            venv_dir = work_root / f"venv-{extra}"
            _checked(
                ["uv", "venv", "--python", "3.13", str(venv_dir)],
                context=f"uv venv for {extra} SBOM",
            )
            python_bin = venv_dir / "bin" / "python"
            _checked(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python_bin),
                    f"{wheel}[{extra}]",
                ],
                context=f"uv pip install for {extra} SBOM",
            )
            listed = _checked(
                [
                    "uv",
                    "pip",
                    "list",
                    "--python",
                    str(python_bin),
                    "--format",
                    "json",
                ],
                context=f"uv pip list for {extra} SBOM",
                timeout=60,
            )
            extras[extra] = json.loads(listed.stdout)
    return {"base": [], "extras": extras}


def _docs_digest() -> dict[str, Any]:
    _checked(["make", "docs"], context="make docs")
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
    except ReleaseEvidenceError as error:
        print(f"build_release_evidence: {error}", file=sys.stderr)
        return 1

    try:
        RELEASE_DIR.mkdir(parents=True, exist_ok=True)
        BUNDLE_PATH.write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as error:
        print(f"build_release_evidence: cannot write bundle: {error}", file=sys.stderr)
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
