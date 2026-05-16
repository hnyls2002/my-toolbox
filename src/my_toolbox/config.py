"""Centralized configuration: env vars, config paths, and shared constants.

Cluster/instance/container topology lives in ``my_toolbox.rdev.topology``;
this module only owns sync-root + git-meta paths.
"""

from __future__ import annotations

import os
from pathlib import Path


class SyncRootNotSetError(RuntimeError):
    pass


# rgit config

RGIT_PROFILES = Path.home() / ".config" / "rgit" / "profiles.yaml"
GIT_META_DIR_NAME = "commit_msg"


# sync log

RDEV_SYNC_LOG = Path.home() / ".rdev" / "sync.log"


def get_sync_root() -> Path:
    """Read SYNC_ROOT from environment. Raises SyncRootNotSetError if unset."""
    sync_root = os.environ.get("SYNC_ROOT")
    if not sync_root:
        raise SyncRootNotSetError(
            "SYNC_ROOT is not set. Add 'export SYNC_ROOT=...' to your shell profile."
        )
    return Path(sync_root)


def get_meta_dir() -> Path:
    """Return the git metadata directory (sync_root / commit_msg)."""
    return get_sync_root() / GIT_META_DIR_NAME
