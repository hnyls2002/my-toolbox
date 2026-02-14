import os
import subprocess
from pathlib import Path
from typing import Optional

GIT_META_DIR_NAME = "commit_msg"
TOP_DIRS = {"common_sync"}
_BASE_SYNC_DIRS = ["scripts", "sglang", "my-toolbox"]


class SyncTree:
    @property
    def sync_root(self) -> Path:
        for start in (Path.cwd(), Path.cwd().resolve()):
            d = start
            while d.as_posix() != "/":
                if d.name in TOP_DIRS:
                    return d
                d = d.parent

        raise FileNotFoundError("Sync root not found")

    @property
    def git_meta_dir(self) -> Path:
        return self.sync_root / GIT_META_DIR_NAME

    @property
    def sync_dirs(self) -> list[str]:
        dirs = list(_BASE_SYNC_DIRS)

        base_set = set(_BASE_SYNC_DIRS)
        for entries in self.discover_worktree_map().values():
            for e in entries:
                if e["name"] not in base_set:
                    dirs.append(e["name"])

        extra = os.environ.get("LSYNC_EXTRA_SYNC_DIRS", "")
        if extra:
            dirs.extend(extra.split(","))
        return dirs

    @property
    def repo_dirs(self) -> list[str]:
        root = self.sync_root
        return [d for d in self.sync_dirs if self.is_git_repo(root / d)]

    @staticmethod
    def is_git_repo(path: Path) -> bool:
        return path.is_dir() and (path / ".git").exists()

    # ------------------------------------------------------------------
    # Worktree discovery
    # ------------------------------------------------------------------

    def discover_worktree_map(self) -> dict[str, list[dict]]:
        """Return {base_repo: [{name, branch, head}, ...]} for all worktrees."""
        root = self.sync_root
        wt_map: dict[str, list[dict]] = {}

        for repo in _BASE_SYNC_DIRS:
            repo_path = root / repo
            if not self.is_git_repo(repo_path):
                continue

            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )

            entries: list[dict] = []
            current: dict = {}
            for line in result.stdout.splitlines():
                if line.startswith("worktree "):
                    if current:
                        entries.append(current)
                    wt_path = Path(line.split(" ", 1)[1])
                    current = {"name": wt_path.name}
                elif line.startswith("HEAD "):
                    current["head"] = line.split(" ", 1)[1][:8]
                elif line.startswith("branch "):
                    ref = line.split(" ", 1)[1]
                    current["branch"] = ref.removeprefix("refs/heads/")

            if current:
                entries.append(current)

            # only include worktrees under sync_root
            entries = [e for e in entries if (root / e["name"]).is_dir()]
            if entries:
                wt_map[repo] = entries

        return wt_map

    def detect_repo_from_cwd(self) -> Optional[str]:
        try:
            root = self.sync_root
        except FileNotFoundError:
            return None

        meta_dir = self.git_meta_dir
        if not meta_dir.is_dir():
            return None

        # NOTE: we only look for repos already collected in git meta dir
        # as not all repos are git repos
        known_repos = {d.name for d in meta_dir.iterdir() if d.is_dir()}
        for start in (Path.cwd(), Path.cwd().resolve()):
            d = start
            while d != root and d.as_posix() != "/":
                if d.name in known_repos and d.parent == root:
                    return d.name
                d = d.parent

        return None
