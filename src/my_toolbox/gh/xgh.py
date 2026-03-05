"""GitHub URL helper — parse any GitHub URL and generate gh CLI commands.

Usage:
    xgh cancel <url>            # print gh run cancel command
    xgh cancel <url> -x         # execute it directly
    xgh cancel <url> -f         # force cancel
    xgh cancel <url> -fx        # force cancel and execute

    xgh-checkout <pr_url_or_number>                   # checkout PR and cd into worktree
    xgh-checkout <pr_url_or_number> --path /tmp/my-wt # custom worktree path
"""

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer

app = typer.Typer(help="GitHub URL helper — parse URLs and generate gh CLI commands.")


@app.callback()
def _callback() -> None:
    """GitHub URL helper — parse URLs and generate gh CLI commands."""


_ACTIONS_RUN_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run_id>\d+)"
    r"(?:/job/(?P<job_id>\d+))?"
)
_PR_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")
_ISSUE_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)")


@dataclass
class GitHubURL:
    owner: str
    repo: str
    type: str
    number: str
    job_id: Optional[str] = None

    @property
    def repo_full(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_github_url(url: str) -> GitHubURL:
    """Parse a GitHub URL into structured components."""
    parsed = urlparse(url)
    path = parsed.path

    m = _ACTIONS_RUN_RE.match(path)
    if m:
        return GitHubURL(
            owner=m.group("owner"),
            repo=m.group("repo"),
            type="run",
            number=m.group("run_id"),
            job_id=m.group("job_id"),
        )

    m = _PR_RE.match(path)
    if m:
        return GitHubURL(
            owner=m.group("owner"),
            repo=m.group("repo"),
            type="pr",
            number=m.group("number"),
        )

    m = _ISSUE_RE.match(path)
    if m:
        return GitHubURL(
            owner=m.group("owner"),
            repo=m.group("repo"),
            type="issue",
            number=m.group("number"),
        )

    typer.echo(f"error: unrecognized GitHub URL: {url}", err=True)
    raise typer.Exit(1)


def _run_or_print(cmd: list[str], execute: bool) -> None:
    """Print a gh command, optionally execute it."""
    typer.echo(" ".join(cmd))
    if execute:
        sys.exit(subprocess.call(cmd))


@app.command()
def cancel(
    url: str = typer.Argument(help="GitHub Actions run URL"),
    execute: bool = typer.Option(False, "-x", "--exec", help="Execute the command"),
    force: bool = typer.Option(False, "-f", "--force", help="Force cancel"),
) -> None:
    """Cancel a GitHub Actions workflow run."""
    gh = parse_github_url(url)
    if gh.type != "run":
        typer.echo(f"error: expected an actions run URL, got {gh.type}", err=True)
        raise typer.Exit(1)

    cmd = ["gh", "run", "cancel", gh.number, "--repo", gh.repo_full]
    if force:
        cmd.append("--force")
    _run_or_print(cmd, execute)


def _git_repo_root() -> Path:
    """Get the root of the current git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        typer.echo("error: not inside a git repository", err=True)
        raise typer.Exit(1)
    return Path(result.stdout.strip())


def _log(msg: str) -> None:
    """Print progress info to stderr so stdout stays clean for machine consumption."""
    typer.echo(msg, err=True)


def _default_worktree_path(repo_root: Path, pr_number: str) -> Path:
    """Place worktree as sibling under sync_root if possible, else under repo."""
    try:
        from my_toolbox.lsync.sync_tree import SyncTree

        sync_root = SyncTree().sync_root
    except FileNotFoundError:
        return repo_root / ".worktrees" / f"pr-{pr_number}"

    if repo_root.parent == sync_root:
        return sync_root / f"{repo_root.name}-pr-{pr_number}"

    return repo_root / ".worktrees" / f"pr-{pr_number}"


def _parse_pr_ref(ref: str) -> tuple[Optional[GitHubURL], str]:
    """Parse a PR reference — either a URL or a bare number.

    Returns (parsed_url_or_None, pr_number_str).
    """
    if ref.startswith("http"):
        gh = parse_github_url(ref)
        if gh.type != "pr":
            typer.echo(f"error: expected a PR URL, got {gh.type}", err=True)
            raise typer.Exit(1)
        return gh, gh.number
    if ref.isdigit():
        return None, ref
    typer.echo(f"error: expected a PR URL or number, got: {ref}", err=True)
    raise typer.Exit(1)


@app.command()
def checkout(
    ref: str = typer.Argument(help="PR URL or number"),
    path: Optional[str] = typer.Option(None, "--path", help="Custom worktree path"),
) -> None:
    """Checkout a PR into a new git worktree."""
    gh_url, pr_number = _parse_pr_ref(ref)

    repo_root = _git_repo_root()
    wt_path = Path(path) if path else _default_worktree_path(repo_root, pr_number)
    wt_path = wt_path.resolve()

    if wt_path.exists():
        _log(f"worktree already exists: {wt_path}")
        typer.echo(str(wt_path))
        raise typer.Exit(0)

    # Create worktree (detached so gh pr checkout can set up the branch)
    _log(f"git worktree add --detach {wt_path}")
    ret = subprocess.call(["git", "worktree", "add", "--detach", str(wt_path)])
    if ret != 0:
        raise typer.Exit(ret)

    # Checkout PR inside the worktree
    repo_flag = ["--repo", gh_url.repo_full] if gh_url else []
    cmd = ["gh", "pr", "checkout", pr_number, *repo_flag]
    _log(" ".join(cmd))
    ret = subprocess.call(cmd, cwd=str(wt_path))
    if ret != 0:
        _log("error: gh pr checkout failed, cleaning up worktree")
        subprocess.call(["git", "worktree", "remove", "--force", str(wt_path)])
        raise typer.Exit(ret)

    typer.echo(str(wt_path))


if __name__ == "__main__":
    app()
