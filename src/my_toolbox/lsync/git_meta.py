"""Collect git metadata (log, status, branch, diff) for whitelisted repos.

The metadata is written to git_meta_dir/<repo>/ as plain text files
so that it can be rsynced to remote servers that lack .git directories.
"""

from __future__ import annotations

import json
import subprocess

from my_toolbox.git.git_meta import GIT_COMMANDS, WORKTREE_MAP_FILE, write_if_changed
from my_toolbox.lsync.sync_tree import SyncTree
from my_toolbox.ui import green_text, section_header


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
            if filename.startswith("log"):
                full_cmd.append(f"-{self.log_limit}")

            result = subprocess.run(
                full_cmd,
                cwd=repo_dir,
                capture_output=True,
                text=True,
            )
            write_if_changed(output_dir / filename, result.stdout)

    def _write_worktree_map(self) -> None:
        wt_map = self.tree.discover_worktree_map()
        if not wt_map:
            return

        meta_dir = self.tree.git_meta_dir
        meta_dir.mkdir(parents=True, exist_ok=True)

        out_path = meta_dir / WORKTREE_MAP_FILE
        write_if_changed(out_path, json.dumps(wt_map, indent=2) + "\n")
        print(f"  {green_text('✓')} {WORKTREE_MAP_FILE}")

    def collect_all(self) -> None:
        print(section_header("Git Metadata"))

        for repo_name in self.tree.repo_dirs:
            output_path = self.tree.git_meta_dir / repo_name
            relative = output_path.relative_to(self.tree.sync_root)
            self.collect_repo(repo_name)
            print(f"  {green_text('✓')} {repo_name:<12} -> {relative}")

        self._write_worktree_map()
