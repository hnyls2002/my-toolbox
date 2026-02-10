"""Remote git metadata viewer.

Usage:
    rgit list                  # list available repos
    rgit log                   # show commit log (auto-detect repo from cwd)
    rgit log sglang            # show commit log for sglang
    rgit status                # show git status
    rgit branch                # show branch info
    rgit diff-stat             # show diff --stat
    rgit diff                  # show full diff
    rgit status-all            # show status summary for all repos
"""

import re
from typing import Optional

import typer

from my_toolbox.lsync.git_meta import GitMetaReader
from my_toolbox.lsync.pager import page
from my_toolbox.lsync.sync_tree import SyncTree

app = typer.Typer(help="Read-only git metadata viewer for remote servers.")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_tree = SyncTree()
_reader = GitMetaReader(_tree)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _resolve_repo(repo: Optional[str]) -> str:
    if repo:
        return repo

    detected = _tree.detect_repo_from_cwd()
    if detected is None:
        typer.echo(
            "Error: cannot detect repo from current directory. "
            "Please specify a repo name explicitly, or cd into a repo.",
            err=True,
        )
        raise typer.Exit(1)
    return detected


def _read_or_exit(repo: str, filename: str) -> str:
    try:
        return _reader.read_file(repo, filename)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("list")
def list_repos():
    """List all repos with cached git metadata."""
    repos = _reader.list_repos()
    if not repos:
        typer.echo("No repos found in commit_msg/.")
        raise typer.Exit(0)

    lines = ["Available repos:"]
    for repo in repos:
        lines.append(f"  - {repo}")
    page("\n".join(lines) + "\n")


@app.command("log")
def show_log(
    repo: Optional[str] = typer.Argument(
        None, help="Repository name (auto-detected from cwd if omitted)"
    ),
    n: int = typer.Option(0, "-n", help="Show only the first N lines (0 = all)"),
):
    """Show the commit log for a repo."""
    repo = _resolve_repo(repo)
    content = _read_or_exit(repo, "log.txt")

    if n > 0:
        lines = content.splitlines()[:n]
        page("\n".join(lines) + "\n")
    else:
        page(content)


@app.command("status")
def show_status(
    repo: Optional[str] = typer.Argument(
        None, help="Repository name (auto-detected from cwd if omitted)"
    ),
):
    """Show the git status for a repo."""
    repo = _resolve_repo(repo)
    page(_read_or_exit(repo, "status.txt"))


@app.command("branch")
def show_branch(
    repo: Optional[str] = typer.Argument(
        None, help="Repository name (auto-detected from cwd if omitted)"
    ),
):
    """Show branch info for a repo."""
    repo = _resolve_repo(repo)
    page(_read_or_exit(repo, "branch.txt"))


@app.command("diff-stat")
def show_diff_stat(
    repo: Optional[str] = typer.Argument(
        None, help="Repository name (auto-detected from cwd if omitted)"
    ),
):
    """Show git diff --stat for a repo."""
    repo = _resolve_repo(repo)
    page(_read_or_exit(repo, "diff_stat.txt"))


@app.command("diff")
def show_diff(
    repo: Optional[str] = typer.Argument(
        None, help="Repository name (auto-detected from cwd if omitted)"
    ),
):
    """Show the full git diff for a repo."""
    repo = _resolve_repo(repo)
    page(_read_or_exit(repo, "diff.txt"))


def _parse_status_lines(status_content: str) -> dict[str, list[str]]:
    """Parse git status output into {"staged": [...], "unstaged": [...],
    "untracked": [...]} with original colored lines."""
    result: dict[str, list[str]] = {
        "staged": [],
        "unstaged": [],
        "untracked": [],
    }
    section = ""

    for line in status_content.splitlines():
        plain = _strip_ansi(line)
        if "Changes to be committed" in plain:
            section = "staged"
        elif "Changes not staged for commit" in plain:
            section = "unstaged"
        elif "Untracked files" in plain:
            section = "untracked"
        elif plain.startswith("\t") and section:
            result[section].append(line)

    return result


@app.command("status-all")
def status_all():
    """Show a compact status summary for all repos."""
    repos = _reader.list_repos()
    if not repos:
        typer.echo("No repos found in commit_msg/.")
        raise typer.Exit(0)

    out: list[str] = []
    for repo in repos:
        out.append(f"\n{'='*60}")
        out.append(f"  {repo}")
        out.append(f"{'='*60}")

        # Current branch (first line starting with '*')
        branch_content = _read_or_exit(repo, "branch.txt")
        for line in branch_content.splitlines():
            if _strip_ansi(line).startswith("*"):
                out.append(f"  Branch: {line.strip()}")
                break

        # Staged / unstaged / untracked files
        status_content = _read_or_exit(repo, "status.txt")
        parsed = _parse_status_lines(status_content)

        for category in ("staged", "unstaged", "untracked"):
            if parsed[category]:
                out.append(f"  {category.capitalize()}:")
                for line in parsed[category]:
                    out.append(f"  {line}")

        if not any(parsed.values()):
            status_lines = status_content.strip().splitlines()
            if status_lines:
                out.append(f"  Status: {status_lines[-1].strip()}")

        # Diff stat summary (last line, e.g. "2 files changed, ...")
        diff_stat_content = _read_or_exit(repo, "diff_stat.txt").strip()
        if diff_stat_content:
            last_line = diff_stat_content.splitlines()[-1].strip()
            out.append(f"  Diff:   {last_line}")

        # Last commit (first non-graph line from log)
        log_content = _read_or_exit(repo, "log.txt")
        for line in log_content.splitlines():
            plain = _strip_ansi(line).strip().lstrip("* |/\\")
            if plain:
                out.append(f"  Latest: {line.strip()}")
                break

    page("\n".join(out) + "\n")


if __name__ == "__main__":
    app()
