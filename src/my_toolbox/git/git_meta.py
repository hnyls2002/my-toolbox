"""Git metadata reader and collection helpers.

Reads pre-collected git metadata (log, status, branch, diff) from
the metadata directory (sync_root / commit_msg / <repo> / *.txt).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

WORKTREE_MAP_FILE = "worktrees.json"

_LOG_FORMAT = (
    "%C(yellow)%h%C(reset) "
    "%C(green)%an%C(reset) "
    "%C(blue)%ad%C(reset) "
    "%s"
    "%C(auto)%d%C(reset)"
)

GIT_COMMANDS: dict[str, list[str]] = {
    "log.txt": [
        "git",
        "log",
        "--color=always",
        f"--pretty=format:{_LOG_FORMAT}",
    ],
    "log_all.txt": [
        "git",
        "log",
        "--all",
        "--graph",
        "--color=always",
        f"--pretty=format:{_LOG_FORMAT}",
    ],
    "status.txt": ["git", "-c", "color.status=always", "status"],
    "branch.txt": ["git", "branch", "-vv", "--color=always"],
    "diff_stat.txt": ["git", "diff", "--stat", "--color=always"],
    "diff.txt": ["git", "diff", "--color=always"],
}


def detect_repo_from_cwd(meta_dir: Path) -> Optional[str]:
    """Infer which repo the cwd belongs to by matching against meta_dir subdirs."""
    if not meta_dir.is_dir():
        return None

    sync_root = meta_dir.parent
    known_repos = {d.name for d in meta_dir.iterdir() if d.is_dir()}

    for start in (Path.cwd(), Path.cwd().resolve()):
        d = start
        while d != sync_root and d.as_posix() != "/":
            if d.name in known_repos and d.parent == sync_root:
                return d.name
            d = d.parent

    return None


def collect_repo(
    repo_name: str, sync_root: Path, meta_dir: Path, log_limit: int = 200
) -> None:
    """Run git commands for a single repo and write output to meta_dir."""
    repo_dir = sync_root / repo_name
    if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
        return

    output_dir = meta_dir / repo_name
    output_dir.mkdir(parents=True, exist_ok=True)

    for filename, cmd in GIT_COMMANDS.items():
        full_cmd = list(cmd)
        if filename.startswith("log"):
            full_cmd.append(f"-{log_limit}")

        result = subprocess.run(
            full_cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        (output_dir / filename).write_text(result.stdout)


class GitMetaReader:
    def __init__(self, meta_dir: Path):
        self.meta_dir = meta_dir

    def list_repos(self) -> list[str]:
        if not self.meta_dir.is_dir():
            return []
        return sorted(
            d.name
            for d in self.meta_dir.iterdir()
            if d.is_dir() and (d / "log.txt").exists()
        )

    def read_worktree_map(self) -> dict[str, list[dict]]:
        wt_file = self.meta_dir / WORKTREE_MAP_FILE
        if not wt_file.exists():
            return {}
        return json.loads(wt_file.read_text())

    def read_file(self, repo: str, filename: str) -> str:
        meta_file = self.meta_dir / repo / filename
        if not meta_file.exists():
            raise FileNotFoundError(f"Metadata file not found: {meta_file}")
        return meta_file.read_text()

    def read_log(self, repo: str) -> str:
        return self.read_file(repo, "log.txt")

    def read_status(self, repo: str) -> str:
        return self.read_file(repo, "status.txt")

    def read_branch(self, repo: str) -> str:
        return self.read_file(repo, "branch.txt")

    def read_diff_stat(self, repo: str) -> str:
        return self.read_file(repo, "diff_stat.txt")

    def read_diff(self, repo: str) -> str:
        return self.read_file(repo, "diff.txt")
