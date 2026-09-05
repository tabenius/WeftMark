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


_SUBPROCESS_TIMEOUT_SECONDS = 300


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd or ROOT,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError) as error:
        raise SmokeInstallError(
            f"command not found: {command[0]} (is it installed and on PATH?)"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise SmokeInstallError(
            f"timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s: {' '.join(command)}"
        ) from error
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

    current_version: str | None = None
    try:
        wheel = build_wheel()
        print(f"built {wheel.relative_to(ROOT)}", flush=True)
        with tempfile.TemporaryDirectory(prefix="weftmark-smoke-") as tmp:
            work_root = Path(tmp)
            for version in SUPPORTED_PYTHON_VERSIONS:
                current_version = version
                print(f"smoke-testing Python {version} ...", flush=True)
                smoke_one_version(version, wheel, work_root)
                print(f"Python {version}: ok", flush=True)
    except SmokeInstallError as error:
        where = f" (Python {current_version})" if current_version else ""
        print(f"smoke_install{where}: {error}", file=sys.stderr)
        return 1

    print(
        f"smoke_install: {len(SUPPORTED_PYTHON_VERSIONS)} Python versions "
        "passed changeset/evidence/review against a clean, no-extras install"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
