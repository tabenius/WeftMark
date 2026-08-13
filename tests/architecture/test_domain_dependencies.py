"""Enforce the dependency direction documented by the WeftMark architecture."""

from __future__ import annotations

import ast
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

