#!/usr/bin/env python3

"""Ricercar — entrypoint.

Usage:
    uv run python -m ricercar
    # or after ``uv sync``:
    ricercar
"""

from __future__ import annotations

import sys


def main() -> None:
    # Lazy-import so that CLI help is instant even without full env
    from ricercar.cli import cli

    cli()


if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    main()
