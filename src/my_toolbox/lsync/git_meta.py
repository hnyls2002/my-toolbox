"""Collect git metadata (log, status, branch, diff) for whitelisted repos.

The metadata is written to git_meta_dir/<repo>/ as plain text files
so that it can be rsynced to remote servers that lack .git directories.
"""

from __future__ import annotations

import subprocess

from my_toolbox.lsync.sync_tree import SyncTree
from my_toolbox.lsync.ui import green_text, section_header

# Color is forced on (--color=always / %C() format) so the cached files
# render with the same coloring as native git when viewed through a pager.
_LOG_FORMAT = (
    "%C(yellow)%h%C(reset) "
    "%C(green)%an%C(reset) "
    "%C(blue)%ad%C(reset) "
    "%s"
    "%C(auto)%d%C(reset)"
)

GIT_COMMANDS = {
    "log.txt": [
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


class GitMetaCollector:
    def __init__(self, tree: SyncTree, log_limit: int = 200):
        self.tree = tree
        self.log_limit = log_limit

    def collect_repo(self, repo_name: str) -> None:
        repo_dir = self.tree.sync_root / repo_name
        if not self.tree.is_git_repo(repo_dir):
            return

        output_dir = self.tree.git_meta_dir / repo_name
        output_dir.mkdir(parents=True, exist_ok=True)

        for filename, cmd in GIT_COMMANDS.items():
            full_cmd = list(cmd)
            if filename == "log.txt":
                full_cmd.append(f"-{self.log_limit}")

            result = subprocess.run(
                full_cmd,
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            (output_dir / filename).write_text(result.stdout)

    def collect_all(self) -> None:
        print(section_header("Git Metadata"))

        for repo_name in self.tree.repo_dirs:
            output_path = self.tree.git_meta_dir / repo_name
            relative = output_path.relative_to(self.tree.sync_root)
            self.collect_repo(repo_name)
            print(f"  {green_text('✓')} {repo_name:<12} -> {relative}")


class GitMetaReader:
    def __init__(self, tree: SyncTree):
        self.tree = tree

    def list_repos(self) -> list[str]:
        meta_dir = self.tree.git_meta_dir
        if not meta_dir.is_dir():
            return []
        return sorted(
            d.name
            for d in meta_dir.iterdir()
            if d.is_dir() and (d / "log.txt").exists()
        )

    def read_file(self, repo: str, filename: str) -> str:
        meta_file = self.tree.git_meta_dir / repo / filename
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
