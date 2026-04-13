"""Accumulation-worktree mode for rdiff.

Build a 0-noise combined diff of multiple PRs by:
  1. Creating a temporary git worktree at a common base commit.
  2. Applying each PR's changes in order (cherry-pick for merged PRs, patch
     apply + commit for open PRs).
  3. Running `git diff <base>..HEAD` inside the worktree.
  4. Cleaning up.
"""

import json
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import typer

from my_toolbox.ui import cyan_text, dim, green_text, red_text


@dataclass
class PRInfo:
    number: int
    state: str  # MERGED / OPEN / CLOSED
    merge_commit: Optional[str]  # sha if merged
    head_ref: str
    base_ref: str
    head_repo: Optional[str]  # owner/name (None if same repo)
    commits: List[str]  # oids of PR commits


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def _gh_repo(cwd: Path) -> str:
    r = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=cwd,
    )
    if r.returncode != 0:
        typer.echo(red_text("Cannot determine GitHub repo via `gh`."), err=True)
        typer.echo(dim(r.stderr), err=True)
        raise typer.Exit(2)
    return r.stdout.strip()


def fetch_pr(number: int, repo: str) -> PRInfo:
    fields = "number,state,mergeCommit,headRefName,baseRefName,headRepository,headRepositoryOwner,commits"
    r = _run(["gh", "pr", "view", str(number), "--repo", repo, "--json", fields])
    if r.returncode != 0:
        typer.echo(red_text(f"Cannot fetch PR #{number} from {repo}"), err=True)
        typer.echo(dim(r.stderr), err=True)
        raise typer.Exit(2)
    data = json.loads(r.stdout)
    merge_commit = (data.get("mergeCommit") or {}).get("oid")
    head_repo_owner = (data.get("headRepositoryOwner") or {}).get("login")
    head_repo_name = (data.get("headRepository") or {}).get("name")
    head_repo = (
        f"{head_repo_owner}/{head_repo_name}"
        if head_repo_owner and head_repo_name
        else None
    )
    commits = [c["oid"] for c in (data.get("commits") or [])]
    return PRInfo(
        number=data["number"],
        state=data["state"],
        merge_commit=merge_commit,
        head_ref=data["headRefName"],
        base_ref=data["baseRefName"],
        head_repo=head_repo,
        commits=commits,
    )


def _ensure_commit_available(sha: str, cwd: Path) -> None:
    """Make sure a commit is present locally; fetch it from origin if not."""
    r = _run(["git", "cat-file", "-e", sha], cwd=cwd)
    if r.returncode == 0:
        return
    # Fetch explicitly.
    typer.echo(dim(f"  fetching commit {sha[:8]} ..."))
    r = _run(["git", "fetch", "origin", sha], cwd=cwd)
    if r.returncode != 0:
        typer.echo(red_text(f"Failed to fetch commit {sha} from origin."), err=True)
        typer.echo(dim(r.stderr), err=True)
        raise typer.Exit(2)


def auto_base(prs: List[PRInfo], cwd: Path) -> str:
    """Find a commit that predates all PRs.

    For each PR:
      - merged (squash): use `merge_commit^1` (main just before squash).
      - open: use `merge-base(head, origin/main)`.
    Then return the earliest of those points (ancestor-wise). Earliest means
    the one that is an ancestor of all others.
    """
    candidates = []
    for pr in prs:
        if pr.state == "MERGED" and pr.merge_commit:
            _ensure_commit_available(pr.merge_commit, cwd)
            candidates.append(f"{pr.merge_commit}^")
        else:
            # Open PR: head exists locally only if checked out; fall back to
            # origin/main as base approximation.
            _run(["git", "fetch", "origin", "main"], cwd=cwd)
            # merge-base(HEAD, origin/main) is a decent proxy when the open
            # branch is the current HEAD.
            r = _run(["git", "merge-base", "HEAD", "origin/main"], cwd=cwd)
            if r.returncode == 0:
                candidates.append(r.stdout.strip())

    if not candidates:
        typer.echo(red_text("Could not determine any base candidate."), err=True)
        raise typer.Exit(2)

    # Resolve each candidate to a concrete sha.
    shas = []
    for c in candidates:
        r = _run(["git", "rev-parse", c], cwd=cwd)
        if r.returncode != 0:
            typer.echo(red_text(f"Cannot resolve {c}"), err=True)
            raise typer.Exit(2)
        shas.append(r.stdout.strip())

    # Pick the earliest: one that is an ancestor of all others.
    for candidate in shas:
        is_ancestor_of_all = True
        for other in shas:
            if candidate == other:
                continue
            r = _run(
                ["git", "merge-base", "--is-ancestor", candidate, other],
                cwd=cwd,
            )
            if r.returncode != 0:
                is_ancestor_of_all = False
                break
        if is_ancestor_of_all:
            return candidate

    # No single earliest -> take merge-base of all.
    r = _run(["git", "merge-base", "--octopus", *shas], cwd=cwd)
    if r.returncode != 0:
        typer.echo(
            red_text("Cannot compute common ancestor across PR bases."), err=True
        )
        raise typer.Exit(2)
    return r.stdout.strip()


@contextmanager
def temp_worktree(base_sha: str, repo_root: Path):
    """Create a git worktree at base_sha, yield its path, then clean up."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    wt_dir = Path(tempfile.gettempdir()) / f"rdiff-accum-{ts}"
    branch = f"rdiff-accum-{ts}"

    r = _run(
        ["git", "worktree", "add", "-b", branch, str(wt_dir), base_sha],
        cwd=repo_root,
    )
    if r.returncode != 0:
        typer.echo(red_text("git worktree add failed:"), err=True)
        typer.echo(r.stderr, err=True)
        raise typer.Exit(2)
    typer.echo(dim(f"  worktree: {wt_dir}"))

    try:
        yield wt_dir
    finally:
        # Abort any in-progress cherry-pick / am before removal.
        _run(["git", "cherry-pick", "--abort"], cwd=wt_dir)
        _run(["git", "am", "--abort"], cwd=wt_dir)
        _run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=repo_root,
        )
        # Delete the temp branch that backed the worktree.
        _run(["git", "branch", "-D", branch], cwd=repo_root)
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)


def _cherry_pick_squash(sha: str, wt: Path, label: str) -> None:
    r = _run(["git", "cherry-pick", "--allow-empty", sha], cwd=wt)
    if r.returncode != 0:
        typer.echo(red_text(f"cherry-pick failed for {label}:"), err=True)
        typer.echo(r.stdout + r.stderr, err=True)
        raise typer.Exit(2)


def _is_merge_commit(sha: str, cwd: Path) -> bool:
    r = _run(["git", "rev-list", "-1", "--parents", sha], cwd=cwd)
    if r.returncode != 0:
        return False
    parts = r.stdout.strip().split()
    return len(parts) > 2  # sha + 2+ parents


def _cherry_pick_range(shas: List[str], wt: Path, label: str) -> None:
    """Cherry-pick a sequence of commits, skipping merge commits.

    shas should already be in oldest-first order.
    """
    for sha in shas:
        _ensure_commit_available(sha, wt)
        if _is_merge_commit(sha, wt):
            typer.echo(dim(f"    skipping merge commit {sha[:8]}"))
            continue
        short = sha[:8]
        r = _run(["git", "cherry-pick", "--allow-empty", sha], cwd=wt)
        if r.returncode != 0:
            typer.echo(
                red_text(f"cherry-pick failed for {label} at {short}:"), err=True
            )
            typer.echo(r.stdout + r.stderr, err=True)
            raise typer.Exit(2)


def apply_pr(pr: PRInfo, repo: str, wt: Path) -> None:
    """Apply a PR's changes to the worktree.

    MERGED PR: cherry-pick the squash (or merge) commit.
    OPEN PR: cherry-pick each commit in `pr.commits`, skipping merge commits
        (so main-merges inside the branch don't re-introduce noise).
    """
    label = f"PR #{pr.number}"
    typer.echo(cyan_text(f"  applying {label} ({pr.state})"))

    if pr.state == "MERGED" and pr.merge_commit:
        _ensure_commit_available(pr.merge_commit, wt)
        _cherry_pick_squash(pr.merge_commit, wt, label)
        return

    if pr.state == "OPEN":
        if not pr.commits:
            typer.echo(red_text(f"{label} has no commits to apply."), err=True)
            raise typer.Exit(2)
        _cherry_pick_range(pr.commits, wt, label)
        return

    typer.echo(
        red_text(f"PR #{pr.number} is in state {pr.state}; only MERGED/OPEN supported.")
    )
    raise typer.Exit(2)


def build_accumulation_diff(
    pr_numbers: List[int],
    base: Optional[str],
    paths: List[str],
    repo_root: Path,
    repo: Optional[str] = None,
) -> tuple[str, str]:
    """Build a 0-noise combined diff for multiple PRs.

    Returns (diff_text, resolved_base_sha).
    """
    if shutil.which("gh") is None:
        typer.echo(red_text("`gh` CLI is required for --prs mode."), err=True)
        raise typer.Exit(2)

    if repo is None:
        repo = _gh_repo(repo_root)

    typer.echo(dim(f"  repo: {repo}"))
    prs = [fetch_pr(n, repo) for n in pr_numbers]

    for pr in prs:
        typer.echo(
            dim(
                f"  PR #{pr.number}: {pr.state}"
                + (f" merged {pr.merge_commit[:8]}" if pr.merge_commit else "")
                + f" head={pr.head_ref}"
            )
        )

    # Determine base.
    if base is None:
        base_sha = auto_base(prs, repo_root)
        typer.echo(dim(f"  auto base: {base_sha[:12]}"))
    else:
        r = _run(["git", "rev-parse", base], cwd=repo_root)
        if r.returncode != 0:
            typer.echo(red_text(f"Cannot resolve base {base}"), err=True)
            raise typer.Exit(2)
        base_sha = r.stdout.strip()

    with temp_worktree(base_sha, repo_root) as wt:
        for pr in prs:
            apply_pr(pr, repo, wt)

        cmd = ["git", "diff", f"{base_sha}..HEAD"]
        if paths:
            cmd.append("--")
            cmd.extend(paths)
        r = _run(cmd, cwd=wt)
        if r.returncode != 0:
            typer.echo(red_text("final git diff failed:"), err=True)
            typer.echo(r.stderr, err=True)
            raise typer.Exit(r.returncode)

        typer.echo(
            green_text(f"  accumulated diff: {len(r.stdout.splitlines())} lines")
        )
        return r.stdout, base_sha
