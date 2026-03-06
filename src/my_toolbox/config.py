"""Centralized configuration: env vars, config paths, and shared constants."""

from __future__ import annotations

import os
from pathlib import Path


class SyncRootNotSetError(RuntimeError):
    pass


# rgit config

RGIT_PROFILES = Path.home() / ".config" / "rgit" / "profiles.yaml"
GIT_META_DIR_NAME = "commit_msg"


# lsync config


LSYNC_CONFIG = Path.home() / ".lsync.yaml"
LSYNC_LOG = Path.home() / ".lsync.log"


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


def _split_csv_env(key: str) -> list[str]:
    """Read a comma-separated env var, strip whitespace, drop blanks."""
    raw = os.environ.get(key, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def get_nda_dirs() -> list[str]:
    """LSYNC_NDA_DIRS: comma-separated list of NDA directories to sync."""
    return _split_csv_env("LSYNC_NDA_DIRS")


def get_extra_sync_dirs() -> list[str]:
    """LSYNC_EXTRA_SYNC_DIRS: comma-separated extra dirs beyond base + worktrees."""
    return _split_csv_env("LSYNC_EXTRA_SYNC_DIRS")


# docker dev config

DOCKER_HOST_HOME = os.environ.get("DOCKER_HOST_HOME", "lsyin")
DOCKER_CONTAINER = os.environ.get("DOCKER_CONTAINER", "lsyin_sgl")
DOCKER_IMAGE = "lmsysorg/sglang:dev"
