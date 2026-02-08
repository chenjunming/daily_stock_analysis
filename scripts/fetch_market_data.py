#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backward-compatible wrapper around stock_data_fetcher CLI."""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stock_data_fetcher.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

