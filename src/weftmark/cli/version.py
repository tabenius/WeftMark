"""``weftmark --version``: identity plus the capabilities actually installed.

The point is to answer "what did I get?" without needing a repository, a ledger,
or any network access. Optional capabilities are detected from importability,
not assumed from the package metadata.
"""

from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
import platform
import shutil
from typing import Any

from weftmark import __version__

FORGE_ADAPTERS = ("github", "gitlab", "gitea", "forgejo", "bitbucket", "azure-devops")


def _module_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def version_payload() -> dict[str, Any]:
    nostoi = _module_version("nostoi")
    textual = _module_version("textual")
    return {
        "version": __version__,
        "python": platform.python_version(),
        "product": "weftmark",
        "capabilities": {
            "core": True,
            "git": True,
            "ledger_jsonl": True,
            "forge": list(FORGE_ADAPTERS),
            "acp_runtime": True,
            "completions": ["bash", "fish"],
            "tui": textual is not None,
            "sqlite": nostoi is not None,
        },
        "optional": {
            "tui": {"installed": textual is not None, "version": textual, "extra": "tui"},
            "sqlite": {"installed": nostoi is not None, "version": nostoi, "extra": "sqlite"},
            "nostoi_cli": shutil.which("nostoi"),
        },
    }


def version_text(payload: dict[str, Any] | None = None) -> str:
    payload = payload or version_payload()
    capabilities = payload["capabilities"]
    optional = payload["optional"]
    lines = [f"WeftMark {payload['version']}", f"  python       {payload['python']}"]
    lines.append("  core         git, jsonl ledger, forge adapters, ACP runtime")
    lines.append(f"  forge        {', '.join(capabilities['forge'])}")
    lines.append(f"  completions  {', '.join(capabilities['completions'])}")
    tui = optional["tui"]
    lines.append(
        f"  tui          {tui['version'] if tui['installed'] else 'not installed'} "
        + ("" if tui["installed"] else "(pip install '.[tui]')")
    )
    sqlite = optional["sqlite"]
    lines.append(
        f"  sqlite       {sqlite['version'] if sqlite['installed'] else 'not installed'} "
        + ("" if sqlite["installed"] else "(pip install '.[sqlite]')")
    )
    cli = optional["nostoi_cli"]
    lines.append(f"  nostoi CLI   {cli or 'not on PATH (optional verifier)'}")
    return "\n".join(lines)
