"""A terminal-sized rendering of the woven WeftMark W.

The SVG logo is a woven ribbon forming a W with an orange shuttle above the
middle peak and weft threads crossing the two valleys. This keeps that silhouette
in ~31 columns so ``weftmark --help`` has a recognizable wordmark. Color follows
the same rules as the rest of the CLI and is computed once per invocation.
"""

from __future__ import annotations

import sys
from typing import Mapping, TextIO

from weftmark.cli.color import color_enabled, paint

_WIDTH = 31

# (columns -> glyph, style). The three top rows are the orange shuttle; the
# middle rows are the blue woven warp; the last two are the weft threads.
_LOGO_ROWS: tuple[tuple[dict[int, str], tuple[str, ...]], ...] = (
    ({13: "╲", 17: "╱"}, ("orange", "bold")),
    ({14: "╲", 16: "╱"}, ("orange", "bold")),
    ({15: "╳"}, ("orange", "bold")),
    ({3: "╲", 13: "╱", 17: "╲", 27: "╱"}, ("weft_blue",)),
    ({4: "╲", 12: "╱", 18: "╲", 26: "╱"}, ("weft_blue",)),
    ({5: "╲", 11: "╱", 19: "╲", 25: "╱"}, ("weft_blue",)),
    ({6: "╲", 10: "╱", 20: "╲", 24: "╱"}, ("weft_blue",)),
    ({7: "╲", 9: "╱", 21: "╲", 23: "╱"}, ("weft_blue",)),
    ({**{col: "═" for col in range(2, 29)}, 8: "╳", 22: "╳"}, ("brand_blue",)),
    ({col: "~" for col in range(3, 28, 2)}, ("dim", "cyan")),
)

_ASCII = str.maketrans({"╲": "\\", "╱": "/", "╳": "X", "═": "="})


def _row(marks: dict[int, str]) -> str:
    chars = [" "] * _WIDTH
    for column, glyph in marks.items():
        chars[column] = glyph
    return "".join(chars).rstrip()


def plain_logo(*, ascii_only: bool = False) -> str:
    """The uncolored banner, one string with embedded newlines."""
    lines = [_row(marks) for marks, _ in _LOGO_ROWS]
    if ascii_only:
        lines = [line.translate(_ASCII) for line in lines]
    return "\n".join(lines)


def _can_encode(stream: TextIO, text: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def render_logo(
    *,
    stream: TextIO | None = None,
    enabled: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """The banner, colored when safe and encoded for the target stream."""
    stream = sys.stdout if stream is None else stream
    use = color_enabled(stream, env) if enabled is None else enabled
    ascii_only = not _can_encode(stream, plain_logo())
    lines = [
        paint(_row(marks).translate(_ASCII) if ascii_only else _row(marks), *styles, enabled=use)
        for marks, styles in _LOGO_ROWS
    ]
    return "\n".join(lines)


def help_banner(
    *,
    stream: TextIO | None = None,
    enabled: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Banner plus a short wordmark, for prepending to argparse help."""
    use = color_enabled(stream, env) if enabled is None else enabled
    art = render_logo(stream=stream, enabled=use, env=env)
    wordmark = paint("WeftMark", "bold", "brand_blue", enabled=use)
    subtitle = paint("weave scope, lineage, evidence and review", "dim", enabled=use)
    return f"{art}\n{wordmark}  {subtitle}\n"
