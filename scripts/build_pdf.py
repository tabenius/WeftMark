#!/usr/bin/env python3
"""Build the A5 WeftMark PDF with WeasyPrint.

HTML is rebuilt from docs/weftmark.mdx unless --html points at an existing
HTML file. The same CSS serves tablet/browser reading and A5 paged media.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from weasyprint import HTML

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = ROOT / "build" / "weftmark.html"
DEFAULT_PDF = ROOT / "build" / "weftmark_A5.pdf"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--output", type=Path, default=DEFAULT_PDF)
    parser.add_argument(
        "--no-rebuild-html",
        action="store_true",
        help="use the existing HTML instead of rebuilding from MDX",
    )
    args = parser.parse_args()

    html_path = args.html.resolve()
    pdf_path = args.output.resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    if not args.no_rebuild_html:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_html.py"), "--output", str(html_path)],
            cwd=ROOT,
            check=True,
        )
    if not html_path.exists():
        raise SystemExit(f"missing HTML input: {html_path}")

    HTML(filename=str(html_path), base_url=str(ROOT)).write_pdf(str(pdf_path))
    print(pdf_path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
