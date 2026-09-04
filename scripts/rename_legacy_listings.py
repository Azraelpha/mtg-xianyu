#!/usr/bin/env python3
"""Compatibility entry point for the superseding mtg-reconcile command."""

import sys
from pathlib import Path


_src = Path(__file__).parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from mtg_xianyu.reconcile import main  # noqa: E402


if __name__ == "__main__":
    main()
