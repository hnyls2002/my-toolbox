"""Remote git metadata viewer.

Usage:
    rgit list                  # list available repos
    rgit log                   # show commit log (current branch, auto-detect repo)
    rgit log sglang            # show commit log for sglang
    rgit log --all             # show all branches with graph
    rgit status                # show git status
    rgit branch                # show branch info
    rgit diff-stat             # show diff --stat
    rgit diff                  # show full diff
    rgit status-all            # show status summary for all repos
    rgit list-tree             # list worktrees for all repos
    rgit list-tree sglang      # list worktrees for sglang
    rgit switch-tree -n sglang sglang-abort-timeout-kit
    rgit switch-tree -n sglang sglang -d python  # explicit subdir
"""

import json
import re
import subprocess
from importlib.metadata import distributions
from pathlib import Path
from typing import Optional

import typer

from my_toolbox.lsync.git_meta import GitMetaReader
from my_toolbox.lsync.pager import page
from my_toolbox.lsync.sync_tree import SyncTree
from my_toolbox.lsync.ui import green_text

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


def _detect_installed_worktrees(sync_root: Path) -> dict[str, str]:
    """Return {worktree_dir_name: package_name} for editable installs under sync_root."""
    installed: dict[str, str] = {}
    for dist in distributions():
        direct_url_text = dist.read_text("direct_url.json")
        if not direct_url_text:
            continue
        try:
            info = json.loads(direct_url_text)
        except (json.JSONDecodeError, ValueError):
            continue
        if not info.get("dir_info", {}).get("editable"):
            continue
        url = info.get("url", "")
        if not url.startswith("file:///"):
            continue
        pkg_path = Path(url.removeprefix("file://"))
        try:
            rel = pkg_path.relative_to(sync_root)
        except ValueError:
            continue
        if rel.parts:
            installed[rel.parts[0]] = dist.metadata["Name"]
    return installed


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
    all_branches: bool = typer.Option(False, "--all", "-a", help="Show all branches"),
    graph: bool = typer.Option(False, "--graph", help="Show commit graph"),
):
    """Show the commit log for a repo."""
    repo = _resolve_repo(repo)
    filename = "log_all.txt" if (all_branches or graph) else "log.txt"
    content = _read_or_exit(repo, filename)

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
        log_content = _read_or_exit(repo, "log_all.txt")
        for line in log_content.splitlines():
            plain = _strip_ansi(line).strip().lstrip("* |/\\")
            if plain:
                out.append(f"  Latest: {line.strip()}")
                break

    page("\n".join(out) + "\n")


# ---------------------------------------------------------------------------
# Worktree commands
# ---------------------------------------------------------------------------


@app.command("list-tree")
def list_tree(
    repo: Optional[str] = typer.Argument(
        None, help="Base repo name (omit to show all)"
    ),
):
    """List available worktrees (from synced metadata)."""
    wt_map = _reader.read_worktree_map()
    if not wt_map:
        typer.echo("No worktree metadata found. Run lsync first.")
        raise typer.Exit(1)

    repos = [repo] if repo else sorted(wt_map.keys())
    installed = _detect_installed_worktrees(_tree.sync_root)
    out: list[str] = []

    for r in repos:
        entries = wt_map.get(r)
        if entries is None:
            typer.echo(f"Error: no worktree info for repo '{r}'", err=True)
            raise typer.Exit(1)

        out.append(f"{r}:")
        for entry in entries:
            name = entry.get("name", "")
            branch = entry.get("branch", "?")
            head = entry.get("head", "")
            prefix = green_text("✓ ") if name in installed else "  "
            out.append(f"  {prefix}{name:<36} {branch:<30} {head}")

    page("\n".join(out) + "\n")


def _resolve_install_path(root: Path, subdir: Optional[str]) -> Path:
    """Resolve the pip-installable directory within a worktree."""
    candidate = root / subdir if subdir else root
    if not candidate.is_dir():
        typer.echo(f"Error: directory not found: {candidate}", err=True)
        raise typer.Exit(1)

    if (
        not (candidate / "pyproject.toml").exists()
        and not (candidate / "setup.py").exists()
    ):
        typer.echo(
            f"Error: no pyproject.toml or setup.py in {candidate}\n"
            f"Use -d/--subdir to specify the installable subdirectory.",
            err=True,
        )
        raise typer.Exit(1)

    return candidate


@app.command("switch-tree")
def switch_tree(
    target: str = typer.Argument(..., help="Target worktree directory name"),
    name: Optional[str] = typer.Option(
        None, "-n", "--name", help="Base repo name (auto-detected from cwd if omitted)"
    ),
    subdir: Optional[str] = typer.Option(
        None,
        "-d",
        "--subdir",
        help="Subdirectory containing pyproject.toml (auto-detected if omitted)",
    ),
):
    """Switch the installed version by pip install -e into a different worktree."""
    wt_map = _reader.read_worktree_map()
    if not wt_map:
        typer.echo("No worktree metadata found. Run lsync first.", err=True)
        raise typer.Exit(1)

    repo = name if name else _resolve_repo(None)

    entries = wt_map.get(repo)
    if entries is None:
        typer.echo(f"Error: no worktree info for repo '{repo}'", err=True)
        raise typer.Exit(1)

    known_names = {e["name"] for e in entries}
    if target not in known_names:
        typer.echo(
            f"Error: '{target}' is not a known worktree of '{repo}'.\n"
            f"Available: {', '.join(sorted(known_names))}",
            err=True,
        )
        raise typer.Exit(1)

    target_root = _tree.sync_root / target
    if not target_root.is_dir():
        typer.echo(f"Error: directory not found: {target_root}", err=True)
        raise typer.Exit(1)

    install_path = _resolve_install_path(target_root, subdir)

    # find branch info for display
    branch = "?"
    for entry in entries:
        if entry["name"] == target:
            branch = entry.get("branch", "?")
            break

    typer.echo(f"Switching to {target} (branch: {branch})")
    typer.echo(f"  pip install --no-build-isolation -e {install_path}\n")

    result = subprocess.run(
        ["pip", "install", "--no-build-isolation", "-e", str(install_path)],
    )
    if result.returncode != 0:
        typer.echo("Error: pip install failed", err=True)
        raise typer.Exit(result.returncode)

    typer.echo(f"\n{green_text('✓')} Now using: {target} ({branch})")


if __name__ == "__main__":
    app()
