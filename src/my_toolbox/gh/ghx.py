"""GitHub URL helper — parse any GitHub URL and generate gh CLI commands.

Usage:
    ghx cancel <url>            # print gh run cancel command
    ghx cancel <url> -x         # execute it directly
    ghx cancel <url> -f         # force cancel
    ghx cancel <url> -fx        # force cancel and execute
"""

import re
import subprocess
import sys
from dataclasses import dataclass
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


if __name__ == "__main__":
    app()
