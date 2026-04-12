"""Centralized configuration: env vars, config paths, and shared constants."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml


class SyncRootNotSetError(RuntimeError):
    pass


# rgit config

RGIT_PROFILES = Path.home() / ".config" / "rgit" / "profiles.yaml"
GIT_META_DIR_NAME = "commit_msg"


# lsync config

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


# rdev config

RDEV_CONFIG = Path.home() / ".rdev" / "config.yaml"

_rdev_cache: Optional[dict[str, Any]] = None


def load_rdev_config() -> dict[str, Any]:
    """Load ~/.rdev/config.yaml, cached after first read."""
    global _rdev_cache
    if _rdev_cache is not None:
        return _rdev_cache
    if not RDEV_CONFIG.exists():
        _rdev_cache = {}
        return _rdev_cache
    with open(RDEV_CONFIG) as f:
        _rdev_cache = yaml.safe_load(f) or {}
    return _rdev_cache


def rdev_defaults() -> dict[str, Any]:
    """Return the defaults section from rdev config."""
    return load_rdev_config().get("defaults", {})


def rdev_server(name: str) -> dict[str, Any]:
    """Return merged config for a server (defaults + per-server overrides)."""
    cfg = load_rdev_config()
    defaults = cfg.get("defaults", {})
    server = cfg.get("servers", {}).get(name)
    if server is None:
        raise ValueError(f"Unknown server: {name}")
    merged = {**defaults, **server}
    return merged


def rdev_servers() -> dict[str, Any]:
    """Return the servers section from rdev config."""
    return load_rdev_config().get("servers", {})
