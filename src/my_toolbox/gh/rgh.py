"""GitHub CLI helper — parse any GitHub URL and generate/execute gh commands.

Usage:
    rgh cancel <url>            # print gh run cancel command
    rgh cancel <url> -x         # execute it directly
    rgh cancel <url> -fx        # force cancel and execute
    rgh checkout <pr>           # checkout PR into a new worktree
    rgh checkout <pr> --path P  # custom worktree path
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from my_toolbox.config import SyncRootNotSetError, get_sync_root
from my_toolbox.gh.url_parser import parse_github_url

app = typer.Typer(
    help="GitHub CLI helper — parse URLs and generate/execute gh commands."
)


@app.callback()
def _callback() -> None:
    """GitHub CLI helper — parse URLs and generate/execute gh commands."""


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
    if gh is None or gh.type != "run":
        typer.echo(f"error: expected an actions run URL, got: {url}", err=True)
        raise typer.Exit(1)

    cmd = ["gh", "run", "cancel", gh.number, "--repo", gh.repo_full]
    if force:
        cmd.append("--force")
    _run_or_print(cmd, execute)


# ---------------------------------------------------------------------------
# checkout — checkout a PR into a git worktree
# ---------------------------------------------------------------------------


def _parse_pr_ref(ref: str) -> tuple[Optional[str], str]:
    """Parse a PR reference (URL or bare number).

    Returns (repo_full_or_None, pr_number_str).
    """
    if ref.startswith("http"):
        gh = parse_github_url(ref)
        if gh is None or gh.type != "pr":
            typer.echo(f"error: not a PR URL: {ref}", err=True)
            raise typer.Exit(1)
        return gh.repo_full, gh.number
    if ref.isdigit():
        return None, ref
    typer.echo(f"error: expected a PR URL or number, got: {ref}", err=True)
    raise typer.Exit(1)


def _git_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        typer.echo("error: not inside a git repository", err=True)
        raise typer.Exit(1)
    return Path(result.stdout.strip())


def _default_worktree_path(repo_root: Path, pr_number: str) -> Path:
    """Place worktree as sibling under sync_root if possible, else under repo."""
    try:
        sync_root = get_sync_root()
    except (RuntimeError, SyncRootNotSetError):
        return repo_root / ".worktrees" / f"pr-{pr_number}"

    if repo_root.parent == sync_root:
        return sync_root / f"{repo_root.name}-pr-{pr_number}"

    return repo_root / ".worktrees" / f"pr-{pr_number}"


@app.command()
def checkout(
    ref: str = typer.Argument(help="PR URL or number"),
    path: Optional[str] = typer.Option(None, "--path", help="Custom worktree path"),
) -> None:
    """Checkout a PR into a new git worktree."""
    repo_full, pr_number = _parse_pr_ref(ref)

    repo_root = _git_repo_root()
    wt_path = Path(path) if path else _default_worktree_path(repo_root, pr_number)
    wt_path = wt_path.resolve()

    if wt_path.exists():
        typer.echo(f"worktree already exists: {wt_path}", err=True)
        typer.echo(str(wt_path))
        raise typer.Exit(0)

    typer.echo(f"git worktree add --detach {wt_path}", err=True)
    ret = subprocess.call(["git", "worktree", "add", "--detach", str(wt_path)])
    if ret != 0:
        raise typer.Exit(ret)

    repo_flag = ["--repo", repo_full] if repo_full else []
    cmd = ["gh", "pr", "checkout", pr_number, *repo_flag]
    typer.echo(" ".join(cmd), err=True)
    ret = subprocess.call(cmd, cwd=str(wt_path))
    if ret != 0:
        typer.echo("error: gh pr checkout failed, cleaning up worktree", err=True)
        subprocess.call(["git", "worktree", "remove", "--force", str(wt_path)])
        raise typer.Exit(ret)

    typer.echo(str(wt_path))


if __name__ == "__main__":
    app()
