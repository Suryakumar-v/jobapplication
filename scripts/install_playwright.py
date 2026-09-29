"""Install the Playwright Chromium browser used by the API service."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])


if __name__ == "__main__":
    raise SystemExit(main())
