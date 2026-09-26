"""Minimal, dependency-free ANSI color for the CLI.

Color is opt-out and safe by default: emitted only to a real terminal, never
when output is piped or redirected, and never when ``NO_COLOR`` is set. The CLI
doubles as a machine surface (``--json``, ledger scraping), so coloring must
never corrupt non-terminal output.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "cyan": "36",
}

# Evidence state / review outcome / readiness word -> styles. One place so
# every human-readable emitter agrees on what green/red/yellow mean.
_STATUS_STYLES = {
    # evidence states
    "passed": ("green",),
    "failed": ("red",),
    "unavailable": ("yellow",),
    "stale": ("yellow",),
    "superseded": ("dim",),
    # review outcomes / readiness
    "ready": ("green",),
    "ready_with_follow_up": ("green",),
    "approved": ("green",),
    "evidence_incomplete": ("yellow",),
    "changes_requested": ("red",),
    "blocked": ("red",),
    "rejected": ("red",),
}


def color_enabled(stream: TextIO | None = None) -> bool:
    """Whether it is safe to emit ANSI color to ``stream`` (default stdout).

    Off unless the stream is a real terminal; ``NO_COLOR`` (set to anything,
    per no-color.org) and ``TERM=dumb`` force it off; ``FORCE_COLOR`` forces it
    on except when ``NO_COLOR`` also wins.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def paint(
    text: str,
    *styles: str,
    stream: TextIO | None = None,
    enabled: bool | None = None,
) -> str:
    """Wrap ``text`` in the given styles, or return it unchanged when color is
    off. ``enabled`` overrides the stream check so a caller can decide once."""
    use = color_enabled(stream) if enabled is None else enabled
    if not use or not styles:
        return text
    codes = ";".join(_CODES[s] for s in styles if s in _CODES)
    if not codes:
        return text
    return f"\033[{codes}m{text}\033[0m"


def status_styles(word: str) -> tuple[str, ...]:
    """Styles for an evidence state / review outcome (empty if unknown)."""
    return _STATUS_STYLES.get(word, ())
