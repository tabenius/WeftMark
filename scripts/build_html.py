#!/usr/bin/env python3
"""Build self-contained WeftMark HTML from docs/weftmark.mdx.

The root MDX document is the publication entrypoint and may include chapter
fragments with comments of the form:

    <!-- include: chapters/01-example.mdx -->

The include step is deliberately tiny and deterministic. After expansion the
source is a conservative MDX-compatible subset: YAML front matter, Markdown,
and raw HTML. Pandoc renders it without executing JSX/JavaScript, and
--embed-resources creates one portable HTML file for tablet reading and
WeasyPrint.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DEFAULT_SOURCE = DOCS / "weftmark.mdx"
DEFAULT_STYLE = DOCS / "weftmark.css"
DEFAULT_OUTPUT = ROOT / "build" / "weftmark.html"
INCLUDE_RE = re.compile(r"<!--\s*include:\s*([^>]+?)\s*-->")


def expand_includes(path: Path, stack: tuple[Path, ...] = ()) -> str:
    path = path.resolve()
    if path in stack:
        chain = " -> ".join(p.name for p in (*stack, path))
        raise RuntimeError(f"MDX include cycle: {chain}")
    text = path.read_text()

    def repl(match: re.Match[str]) -> str:
        target = (path.parent / match.group(1).strip()).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"missing MDX include: {target}")
        try:
            target.relative_to(DOCS.resolve())
        except ValueError as exc:
            raise RuntimeError(f"include escapes docs/: {target}") from exc
        return expand_includes(target, (*stack, path))

    return INCLUDE_RE.sub(repl, text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--style", type=Path, default=DEFAULT_STYLE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise SystemExit("pandoc is required (for example: apt install pandoc).")

    source = args.source.resolve()
    style = args.style.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_figures.py")], cwd=ROOT, check=True)
    expanded = expand_includes(source)

    with tempfile.NamedTemporaryFile("w", suffix=".mdx", dir=DOCS, delete=False) as tmp:
        tmp.write(expanded)
        tmp_path = Path(tmp.name)
    try:
        cmd = [
            pandoc,
            tmp_path.name,
            "--from=gfm+raw_html",
            "--to=html5",
            "--standalone",
            "--embed-resources",
            f"--css={style.name}",
            "--metadata=pagetitle:From RAGBAZ Frog to WeftMark",
            f"--output={output}",
        ]
        subprocess.run(cmd, cwd=DOCS, check=True)
    finally:
        tmp_path.unlink(missing_ok=True)

    print(output.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
