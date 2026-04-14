"""Storage layout for rdiff.

All generated HTML files live under `RDIFF_HOME` (default `~/.rdiff/html/`).
Users can override individual outputs with `--out`; those are not managed
here and won't show up in `rdiff list` / `prune`.
"""

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List


def rdiff_home() -> Path:
    override = os.environ.get("RDIFF_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".rdiff"


def html_dir() -> Path:
    d = rdiff_home() / "html"
    d.mkdir(parents=True, exist_ok=True)
    return d


def worktrees_dir() -> Path:
    """Persistent saga worktrees live under `~/.rdiff/worktrees/<repo>-saga-<topic>/`."""
    d = rdiff_home() / "worktrees"
    d.mkdir(parents=True, exist_ok=True)
    return d


_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_name(name: str) -> str:
    """Validate a user-supplied --name. Raises ValueError if malformed.

    Allowed chars: letters, digits, `.`, `_`, `-`. No path separators.
    Must contain at least one alphanumeric, and cannot be `.` or `..`
    (would produce `..html` / `...html`, which look like hidden files).
    """
    name = name.strip()
    if not name:
        raise ValueError("--name cannot be empty")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"--name must match [A-Za-z0-9._-]+ (got {name!r})")
    if name in (".", ".."):
        raise ValueError("--name cannot be '.' or '..'")
    if not re.search(r"[A-Za-z0-9]", name):
        raise ValueError(
            f"--name must contain at least one alphanumeric (got {name!r})"
        )
    if name.endswith(".html"):
        name = name[:-5]
    return name


def output_path(name: str) -> Path:
    """Compute the managed output path for a --name."""
    return html_dir() / f"{validate_name(name)}.html"


@dataclass
class StoredHtml:
    path: Path
    size: int
    mtime: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.mtime


def list_html() -> List[StoredHtml]:
    entries: List[StoredHtml] = []
    for p in sorted(html_dir().glob("*.html")):
        try:
            st = p.stat()
            entries.append(StoredHtml(path=p, size=st.st_size, mtime=st.st_mtime))
        except OSError:
            continue
    entries.sort(key=lambda e: e.mtime, reverse=True)
    return entries


@dataclass
class StoredWorktree:
    """A git worktree managed by rdiff (persistent saga or stale accum)."""

    path: Path  # the worktree dir on disk
    kind: str  # "saga" | "accum"
    repo_root: Path  # the main repo this worktree is linked to
    branch: str  # the branch name checked out in the worktree
    mtime: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.mtime


def _worktree_info(wt_path: Path) -> tuple[Path, str] | None:
    """Return (repo_root, branch) for a git worktree dir, or None if invalid."""
    import subprocess

    try:
        common = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    repo_root = Path(common).parent.resolve()
    return repo_root, head


def list_saga_worktrees() -> List[StoredWorktree]:
    """Scan ~/.rdiff/worktrees/ for persistent saga worktrees."""
    entries: List[StoredWorktree] = []
    base = worktrees_dir()
    for child in sorted(base.iterdir()) if base.exists() else []:
        if not child.is_dir():
            continue
        info = _worktree_info(child)
        if info is None:
            continue
        repo_root, branch = info
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        entries.append(
            StoredWorktree(
                path=child,
                kind="saga",
                repo_root=repo_root,
                branch=branch,
                mtime=mtime,
            )
        )
    entries.sort(key=lambda e: e.mtime, reverse=True)
    return entries


def list_accum_worktrees(repo_root: Path) -> List[StoredWorktree]:
    """Scan a specific repo for stale rdiff-accum-* worktrees."""
    import subprocess

    entries: List[StoredWorktree] = []
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return entries

    current_wt: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current_wt = line.split(" ", 1)[1]
        elif line.startswith("branch ") and current_wt is not None:
            branch = line.split("refs/heads/", 1)[-1]
            if branch.startswith("rdiff-accum-"):
                wt_path = Path(current_wt)
                try:
                    mtime = wt_path.stat().st_mtime if wt_path.exists() else time.time()
                except OSError:
                    mtime = time.time()
                entries.append(
                    StoredWorktree(
                        path=wt_path,
                        kind="accum",
                        repo_root=repo_root,
                        branch=branch,
                        mtime=mtime,
                    )
                )
            current_wt = None
    entries.sort(key=lambda e: e.mtime, reverse=True)
    return entries


def delete_worktree(wt: StoredWorktree) -> tuple[bool, str]:
    """Remove a worktree + its branch. Returns (success, message)."""
    import subprocess

    # git worktree remove --force
    r = subprocess.run(
        ["git", "-C", str(wt.repo_root), "worktree", "remove", "--force", str(wt.path)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 and wt.path.exists():
        return False, f"worktree remove failed: {r.stderr.strip()}"

    # git branch -D <branch>
    r = subprocess.run(
        ["git", "-C", str(wt.repo_root), "branch", "-D", wt.branch],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False, f"branch -D failed: {r.stderr.strip()}"
    return True, "ok"


_AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_age(spec: str) -> float:
    """Parse `7d`, `2h`, `30m`, `3600s`, `1w` into seconds."""
    m = re.fullmatch(r"(\d+)([smhdw])", spec.strip())
    if not m:
        raise ValueError(f"Invalid age spec: {spec!r}. Use e.g. 7d, 24h, 30m.")
    n = int(m.group(1))
    return n * _AGE_UNITS[m.group(2)]


def format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"
