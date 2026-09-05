"""Allow `python -m weftmark.tui` as an alternative to the weftmark-tui script."""

from __future__ import annotations

import sys

from weftmark.tui.app import main

if __name__ == "__main__":
    sys.exit(main())
