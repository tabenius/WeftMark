"""Enforce the dependency direction documented by the WeftMark architecture."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import weftmark
import weftmark.application
import weftmark.domain


ROOT = Path(__file__).parents[2]
DOMAIN_ROOT = ROOT / "src" / "weftmark" / "domain"

# Domain code expresses product rules. These modules belong behind application
# ports or in adapters and must never become implicit domain dependencies.
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "git",
        "github",
        "httpx",
        "mcp",
        "requests",
        "sqlite3",
        "weftmark.adapters",
        "weftmark.interfaces",
    }
)


def imported_modules(source: str) -> set[str]:
    """Return absolute import targets found in Python source."""

    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def forbidden_imports(source: str) -> set[str]:
    """Return imports that cross a forbidden domain boundary."""

    return {
        module
        for module in imported_modules(source)
        if any(
            module == forbidden or module.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_IMPORT_ROOTS
        )
    }


def test_package_layers_are_importable() -> None:
    assert weftmark.__version__ == "0.0.1"
    assert weftmark.domain.__name__ == "weftmark.domain"
    assert weftmark.application.__name__ == "weftmark.application"


def test_domain_does_not_import_adapters_or_infrastructure() -> None:
    violations: dict[str, set[str]] = {}
    for path in sorted(DOMAIN_ROOT.rglob("*.py")):
        imports = forbidden_imports(path.read_text(encoding="utf-8"))
        if imports:
            violations[str(path.relative_to(ROOT))] = imports

    assert violations == {}, f"domain dependency violations: {violations}"


def test_boundary_check_recognizes_forbidden_imports() -> None:
    source = """
import sqlite3
from github.client import Client
from weftmark.adapters.git_local import LocalGit
"""

    assert forbidden_imports(source) == {
        "github.client",
        "sqlite3",
        "weftmark.adapters.git_local",
    }


def test_base_cli_import_does_not_pull_textual() -> None:
    """The base `weftmark` CLI must stay importable without the `tui` extra.

    `weftmark.tui.app`'s import of Textual is local to the `tui` subcommand's
    dispatch branch specifically so that `import weftmark.cli.main` never
    imports Textual. Nothing else exercises this guarantee, so a regression
    (e.g. a top-level `from weftmark.tui.app import run_tui`) would otherwise
    go unnoticed — the CI test extra already installs `textual`, so an
    accidental top-level import wouldn't fail any other existing test.
    """

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import weftmark.cli.main; "
            "print(any(m.split('.')[0] == 'textual' for m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"

