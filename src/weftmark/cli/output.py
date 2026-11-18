"""Environment-aware output helpers shared by the CLI.

Keeps machine output pure (``--json`` is one JSON document on stdout, no color)
while letting human output honor the usual terminal conventions: pagers,
editors and color preferences. Executables are always run from an argv list —
``PAGER``/``EDITOR`` are never handed to a shell.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

DEFAULT_PAGER = "less -FRX"
DEFAULT_EDITOR = "vi"


def json_line(payload: Any, *, stream: TextIO | None = None) -> None:
    """Emit exactly one deterministic JSON document to ``stream``."""
    print(json.dumps(payload, sort_keys=True), file=sys.stdout if stream is None else stream)


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _argv(value: str, fallback: str) -> list[str]:
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = []
    if not parts or any("\n" in part or "\x00" in part for part in parts):
        parts = shlex.split(fallback)
    return parts


def pager_command(env: Mapping[str, str] | None = None) -> list[str] | None:
    """Resolve ``WEFTMARK_PAGER`` > ``PAGER`` > ``less -FRX``.

    An explicitly empty variable disables paging. The result is an argv list,
    suitable for ``subprocess.run(..., shell=False)``.
    """
    env = _env(env)
    if "WEFTMARK_PAGER" in env:
        value = env["WEFTMARK_PAGER"]
        if not value.strip():
            return None
    else:
        value = env.get("PAGER", "")
        if not value.strip():
            return DEFAULT_PAGER.split() if "PAGER" not in env else None
    return _argv(value, DEFAULT_PAGER)


def editor_command(env: Mapping[str, str] | None = None) -> list[str]:
    """Resolve ``WEFTMARK_EDITOR`` > ``VISUAL`` > ``EDITOR`` > ``vi``."""
    env = _env(env)
    value = (
        env.get("WEFTMARK_EDITOR", "").strip()
        or env.get("VISUAL", "").strip()
        or env.get("EDITOR", "").strip()
        or DEFAULT_EDITOR
    )
    return _argv(value, DEFAULT_EDITOR)


def _isatty(stream: TextIO | None) -> bool:
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def page_text(
    text: str,
    *,
    stream: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Print ``text``, or feed it to the configured pager on a real terminal."""
    stream = sys.stdout if stream is None else stream
    command = pager_command(env)
    if command is None or not _isatty(stream):
        print(text, end="", file=stream)
        return
    try:
        subprocess.run(command, input=text, text=True, check=False)
    except (OSError, ValueError):
        # A missing or broken pager must never lose the user's output.
        print(text, end="", file=stream)


def edit_text(
    initial: str = "",
    *,
    env: Mapping[str, str] | None = None,
    suffix: str = ".txt",
    prefix: str = "weftmark-",
) -> str | None:
    """Open ``$WEFTMARK_EDITOR`` on a temporary file and return its content.

    Returns ``None`` when the editor could not be started. A zero-length result
    means the user deliberately discarded the draft.
    """
    command = editor_command(env)
    with tempfile.NamedTemporaryFile(
        mode="w+", encoding="utf-8", prefix=prefix, suffix=suffix, delete=False
    ) as handle:
        handle.write(initial)
        handle.flush()
        path = Path(handle.name)
    try:
        result = subprocess.run([*command, str(path)], check=False)
        if result.returncode != 0:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    finally:
        path.unlink(missing_ok=True)


def available(executable: str) -> bool:
    return shutil.which(executable) is not None
