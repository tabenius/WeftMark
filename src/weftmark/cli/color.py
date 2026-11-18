"""Dependency-free, environment-aware ANSI color for the CLI.

Color is opt-out and safe by default: emitted only to a real terminal, never
when output is piped or redirected, and never when ``NO_COLOR`` is set. The CLI
doubles as a machine surface (``--json``, ledger scraping), so coloring must
never corrupt non-terminal output.

Environment (highest precedence first):

* ``NO_COLOR`` (any value, per no-color.org) — always disable.
* ``WEFTMARK_COLOR=never|auto|always`` — explicit WeftMark preference.
* ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` (non-zero) — force on for legacy tools.
* ``TERM=dumb`` or ``CLICOLOR=0`` — disable.
* otherwise — enable only for a real terminal.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, TextIO

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "italic": "3",
    "underline": "4",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_blue": "94",
    "bright_cyan": "96",
    "bright_white": "97",
    # 256-color accents sampled from assets/weftmark.svg. Guarded so a dumber
    # terminal still gets the nearest basic color instead of a broken sequence.
    "orange": "38;5;208",     # #F18420 — the shuttle accent
    "brand_blue": "38;5;25",  # ~#0A3266 — the dark woven warp
    "weft_blue": "38;5;68",   # ~#2B65A5 — the lighter weft
}

_OFF = frozenset({"0", "false", "no", "off"})

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
    # task and lifecycle states
    "todo": ("dim",),
    "in_progress": ("weft_blue",),
    "review": ("yellow",),
    "done": ("green",),
    "completed": ("green",),
    "active": ("weft_blue",),
    "merged": ("green",),
    "closed": ("dim",),
    "abandoned": ("dim",),
    "claimed": ("weft_blue",),
}


def color_preference(env: Mapping[str, str] | None = None) -> bool | None:
    """``True``/``False`` for an explicit WEFTMARK_COLOR, else ``None`` (auto)."""
    env = os.environ if env is None else env
    value = env.get("WEFTMARK_COLOR", "").strip().lower()
    if value in {"always", "on", "true", "1"}:
        return True
    if value in {"never", "off", "false", "0"}:
        return False
    return None


def _on(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() not in _OFF | {""}


def color_enabled(stream: TextIO | None = None, env: Mapping[str, str] | None = None) -> bool:
    """Whether it is safe to emit ANSI color to ``stream`` (default stdout)."""
    env = os.environ if env is None else env
    if "NO_COLOR" in env:
        return False
    preference = color_preference(env)
    if preference is not None:
        return preference
    if _on(env, "FORCE_COLOR") or _on(env, "CLICOLOR_FORCE"):
        return True
    if env.get("TERM") == "dumb":
        return False
    if env.get("CLICOLOR", "").strip().lower() in _OFF:
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


def status_paint(word: str, *, stream: TextIO | None = None, enabled: bool | None = None) -> str:
    """Color a state/outcome word using the shared status palette."""
    return paint(word, *status_styles(word), stream=stream, enabled=enabled)
