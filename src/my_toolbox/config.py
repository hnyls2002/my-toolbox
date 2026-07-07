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


# rdev runtime config (topology + sync); loaded by my_toolbox.rdev.topology and
# by get_base_sync_dirs below.
RDEV_CONFIG = Path.home() / ".rdev" / "config.yaml"


# sync log

RDEV_SYNC_LOG = Path.home() / ".rdev" / "sync.log"


# Fallback base repos under SYNC_ROOT whose worktrees are auto-discovered. Used
# only when RDEV_CONFIG has no `sync.base_dirs`; that config key, when present,
# is the source of truth (see get_base_sync_dirs) -- so registering a new
# worktree-repo is a per-machine config edit, not a code change.
DEFAULT_BASE_SYNC_DIRS = ["scripts", "sglang", "my-toolbox", "sgl-eval"]


def get_base_sync_dirs() -> list[str]:
    """Return the base sync repos (deduped, order preserved).

    ``sync.base_dirs`` in RDEV_CONFIG is the source of truth: when present it is
    the full list of base repos, so it fully replaces DEFAULT_BASE_SYNC_DIRS. A
    missing config file or missing key falls back to the defaults, keeping the
    historical behavior on a machine without the ``sync`` section.
    """
    import yaml  # lazy: keeps config's import graph light for rgit et al.

    try:
        with open(RDEV_CONFIG) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raw = {}
    configured = (raw.get("sync") or {}).get("base_dirs")
    if not configured:
        return list(DEFAULT_BASE_SYNC_DIRS)

    seen: set[str] = set()
    dirs: list[str] = []
    for d in configured:
        if d not in seen:
            seen.add(d)
            dirs.append(d)
    return dirs


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
