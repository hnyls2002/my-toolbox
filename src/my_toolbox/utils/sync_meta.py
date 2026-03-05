"""Shared helpers for locating the sync workspace and git metadata directory."""

from __future__ import annotations

import os
from pathlib import Path

GIT_META_DIR_NAME = "commit_msg"


def get_sync_root() -> Path:
    """Read SYNC_ROOT from environment. Raises if not set."""
    sync_root = os.environ.get("SYNC_ROOT")
    if not sync_root:
        raise RuntimeError(
            "SYNC_ROOT is not set. " "Add 'export SYNC_ROOT=...' to your shell profile."
        )
    return Path(sync_root)


def get_meta_dir() -> Path:
    """Return the git metadata directory (sync_root / commit_msg)."""
    return get_sync_root() / GIT_META_DIR_NAME
