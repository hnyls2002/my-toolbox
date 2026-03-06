"""Unified git toolkit — metadata viewer + identity switcher.

Usage:
    rgit log                   # commit log (current branch, auto-detect repo)
    rgit status                # git status
    rgit branch                # branch info
    rgit diff-stat             # diff --stat
    rgit diff                  # full diff
    rgit repo list             # list available repos
    rgit repo status           # status summary for all repos
    rgit tree list             # list worktrees
    rgit tree install          # switch installed worktree
    rgit tree cd <pr>          # checkout PR into a new worktree
    rgit collect               # refresh git metadata
    rgit id show               # show current identity
    rgit id list               # list profiles
    rgit id use <profile>      # switch identity
"""

import json
import re
import subprocess
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import typer
import yaml

from my_toolbox.config import (
    RGIT_PROFILES,
    SyncRootNotSetError,
    get_meta_dir,
    get_sync_root,
)
from my_toolbox.git.git_meta import GitMetaReader, detect_repo_from_cwd
from my_toolbox.ui import bold, cyan_text, dim, green_text, red_text, yellow_text
from my_toolbox.utils.pager import page

app = typer.Typer(help="Unified git toolkit (metadata viewer + identity switcher).")

# ---------------------------------------------------------------------------
# Shared state (remote git metadata) — lazy so commands that don't need
# SYNC_ROOT (e.g. `rgit id show`) still work without it.
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_meta_dir: Path | None = None
_reader: GitMetaReader | None = None


def _require_meta() -> tuple[Path, GitMetaReader]:
    """Return (meta_dir, reader), initialising on first call."""
    global _meta_dir, _reader
    if _meta_dir is None:
        try:
            _meta_dir = get_meta_dir()
        except SyncRootNotSetError:
            typer.echo(
                f"\n  {red_text('✗')} {bold('SYNC_ROOT')} is not set\n\n"
                f"  Run once in your shell:\n\n"
                f"    {cyan_text('export SYNC_ROOT=/path/to/workspace')}\n\n"
                f"  Or add the line above to {dim('~/.zshrc')} / {dim('~/.bashrc')}\n",
                err=True,
            )
            raise typer.Exit(1)
        _reader = GitMetaReader(_meta_dir)
    assert _reader is not None
    return _meta_dir, _reader


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _resolve_repo(repo: Optional[str]) -> str:
    if repo:
        return repo
    meta_dir, _ = _require_meta()
    detected = detect_repo_from_cwd(meta_dir)
    if detected is None:
        typer.echo(
            "Error: cannot detect repo from current directory. "
            "Please specify a repo name explicitly, or cd into a repo.",
            err=True,
        )
        raise typer.Exit(1)
    return detected


def _read_or_exit(repo: str, filename: str) -> str:
    _, reader = _require_meta()
    try:
        return reader.read_file(repo, filename)
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
# Flat commands (single-repo operations)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# repo sub-app (multi-repo operations)
# ---------------------------------------------------------------------------

repo_app = typer.Typer(help="Multi-repo operations.")


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


@repo_app.command("list")
def repo_list():
    """List all repos with cached git metadata."""
    _, reader = _require_meta()
    repos = reader.list_repos()
    if not repos:
        typer.echo("No repos found in commit_msg/.")
        raise typer.Exit(0)

    lines = ["Available repos:"]
    for repo in repos:
        lines.append(f"  - {repo}")
    page("\n".join(lines) + "\n")


@repo_app.command("status")
def repo_status():
    """Show a compact status summary for all repos."""
    _, reader = _require_meta()
    repos = reader.list_repos()
    if not repos:
        typer.echo("No repos found in commit_msg/.")
        raise typer.Exit(0)

    out: list[str] = []
    for repo in repos:
        out.append(f"\n{'='*60}")
        out.append(f"  {repo}")
        out.append(f"{'='*60}")

        branch_content = _read_or_exit(repo, "branch.txt")
        for line in branch_content.splitlines():
            if _strip_ansi(line).startswith("*"):
                out.append(f"  Branch: {line.strip()}")
                break

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

        diff_stat_content = _read_or_exit(repo, "diff_stat.txt").strip()
        if diff_stat_content:
            last_line = diff_stat_content.splitlines()[-1].strip()
            out.append(f"  Diff:   {last_line}")

        log_content = _read_or_exit(repo, "log_all.txt")
        for line in log_content.splitlines():
            plain = _strip_ansi(line).strip().lstrip("* |/\\")
            if plain:
                out.append(f"  Latest: {line.strip()}")
                break

    page("\n".join(out) + "\n")


app.add_typer(repo_app, name="repo")

# ---------------------------------------------------------------------------
# collect command
# ---------------------------------------------------------------------------


@app.command("collect")
def collect(
    repo: Optional[str] = typer.Argument(
        None, help="Repo name to collect (omit for all)"
    ),
) -> None:
    """Refresh git metadata for repos under sync_root."""
    from my_toolbox.git.git_meta import collect_repo

    meta_dir, _ = _require_meta()
    sync_root = meta_dir.parent

    if repo:
        collect_repo(repo, sync_root, meta_dir)
        typer.echo(f"{green_text('✓')} {repo}")
    else:
        if not sync_root.is_dir():
            typer.echo(f"error: SYNC_ROOT does not exist: {sync_root}", err=True)
            raise typer.Exit(1)

        repos = sorted(
            d.name for d in sync_root.iterdir() if d.is_dir() and (d / ".git").exists()
        )
        if not repos:
            typer.echo("No git repos found under SYNC_ROOT.")
            raise typer.Exit(0)

        for r in repos:
            collect_repo(r, sync_root, meta_dir)
            typer.echo(f"{green_text('✓')} {r}")


# ---------------------------------------------------------------------------
# tree sub-app (worktree operations)
# ---------------------------------------------------------------------------

tree_app = typer.Typer(help="Worktree operations.")


@tree_app.command("list")
def tree_list(
    repo: Optional[str] = typer.Argument(
        None, help="Base repo name (omit to show all)"
    ),
):
    """List available worktrees (from synced metadata)."""
    _, reader = _require_meta()
    wt_map = reader.read_worktree_map()
    if not wt_map:
        typer.echo("No worktree metadata found. Run lsync first.")
        raise typer.Exit(1)

    repos = [repo] if repo else sorted(wt_map.keys())
    installed = _detect_installed_worktrees(get_sync_root())
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


@tree_app.command("install")
def tree_install(
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
    _, reader = _require_meta()
    wt_map = reader.read_worktree_map()
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

    target_root = get_sync_root() / target
    if not target_root.is_dir():
        typer.echo(f"Error: directory not found: {target_root}", err=True)
        raise typer.Exit(1)

    install_path = _resolve_install_path(target_root, subdir)

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


# -- tree cd helpers --------------------------------------------------------

_PR_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")


def _parse_pr_ref(ref: str) -> tuple[Optional[str], str]:
    """Parse a PR reference (URL or bare number).

    Returns (repo_full_or_None, pr_number_str).
    """
    if ref.startswith("http"):
        parsed = urlparse(ref)
        m = _PR_URL_RE.match(parsed.path)
        if not m:
            typer.echo(f"error: not a PR URL: {ref}", err=True)
            raise typer.Exit(1)
        repo_full = f"{m.group('owner')}/{m.group('repo')}"
        return repo_full, m.group("number")
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
    except RuntimeError:
        return repo_root / ".worktrees" / f"pr-{pr_number}"

    if repo_root.parent == sync_root:
        return sync_root / f"{repo_root.name}-pr-{pr_number}"

    return repo_root / ".worktrees" / f"pr-{pr_number}"


@tree_app.command("cd")
def tree_cd(
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


app.add_typer(tree_app, name="tree")

# ---------------------------------------------------------------------------
# id sub-app (identity management)
# ---------------------------------------------------------------------------

id_app = typer.Typer(help="Git identity management.")


@dataclass
class _Profile:
    name: str
    email: str
    gh_user: Optional[str] = None


def _load_profiles() -> Dict[str, _Profile]:
    if not RGIT_PROFILES.exists():
        return {}
    raw = yaml.safe_load(RGIT_PROFILES.read_text()) or {}
    return {
        key: _Profile(name=val["name"], email=val["email"], gh_user=val.get("gh_user"))
        for key, val in raw.get("profiles", {}).items()
    }


def _save_profiles(profiles: Dict[str, _Profile]) -> None:
    RGIT_PROFILES.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "profiles": {
            key: {
                "name": p.name,
                "email": p.email,
                **({"gh_user": p.gh_user} if p.gh_user else {}),
            }
            for key, p in profiles.items()
        }
    }
    RGIT_PROFILES.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True)
    )


def _git_config_get(key: str, *, scope: Optional[str] = None) -> Optional[str]:
    cmd = ["git", "config"]
    if scope:
        cmd.append(f"--{scope}")
    cmd.append(key)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _git_config_set(key: str, value: str, *, is_global: bool = False) -> None:
    cmd = ["git", "config"]
    if is_global:
        cmd.append("--global")
    cmd.extend([key, value])
    subprocess.run(cmd, check=True)


def _match_profile(
    profiles: Dict[str, _Profile], email: Optional[str]
) -> Optional[str]:
    if not email:
        return None
    for key, p in profiles.items():
        if p.email == email:
            return key
    return None


def _format_identity(
    name: Optional[str], email: Optional[str], profiles: Dict[str, _Profile]
) -> str:
    matched = _match_profile(profiles, email)
    identity = f"{name or '(not set)'} <{email or '(not set)'}>"
    if matched:
        return f"{bold(identity)}  {green_text(matched)}"
    return bold(identity)


def _gh_active_user() -> Optional[str]:
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    output = result.stdout + result.stderr
    for line in output.splitlines():
        if "Logged in to" in line and "Active account: true" not in line:
            parts = line.strip().split("account ")
            if len(parts) > 1:
                return parts[1].split()[0].strip()
    return None


@id_app.command("show")
def id_show() -> None:
    """Show the current repo's git identity."""
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    repo_path = (
        toplevel.stdout.strip() if toplevel.returncode == 0 else "(not a git repo)"
    )

    profiles = _load_profiles()

    local_name = _git_config_get("user.name", scope="local")
    local_email = _git_config_get("user.email", scope="local")
    global_name = _git_config_get("user.name", scope="global")
    global_email = _git_config_get("user.email", scope="global")

    gh_user = _gh_active_user()

    typer.echo(f"repo:    {cyan_text(repo_path)}")
    typer.echo(f"gh:      {bold(gh_user) if gh_user else dim('(not logged in)')}")

    if local_name or local_email:
        typer.echo(f"local:   {_format_identity(local_name, local_email, profiles)}")
    else:
        typer.echo(f"local:   {dim('(not set)')}")

    typer.echo(dim(f"global:  {global_name or '?'} <{global_email or '?'}>"))


@id_app.command("list")
def id_list() -> None:
    """List all configured profiles."""
    profiles = _load_profiles()
    if not profiles:
        typer.echo("No profiles configured. Use 'rgit id add' to create one.")
        raise typer.Exit()

    cur_email = _git_config_get("user.email")
    matched = _match_profile(profiles, cur_email)

    for key, p in profiles.items():
        marker = green_text("* ") if key == matched else "  "
        label = bold(key)
        typer.echo(f"{marker}{label}  {p.name} <{p.email}>")


@id_app.command("use")
def id_use(
    profile: str = typer.Argument(help="Profile name to switch to"),
    is_global: bool = typer.Option(False, "--global", "-g", help="Set globally"),
) -> None:
    """Switch git identity to a profile."""
    profiles = _load_profiles()
    if profile not in profiles:
        typer.echo(f"error: profile '{profile}' not found", err=True)
        typer.echo(f"available: {', '.join(profiles.keys())}", err=True)
        raise typer.Exit(1)

    p = profiles[profile]
    scope = "global" if is_global else "local"
    _git_config_set("user.name", p.name, is_global=is_global)
    _git_config_set("user.email", p.email, is_global=is_global)
    typer.echo(f"Switched to {green_text(profile)} ({scope}): {p.name} <{p.email}>")

    if p.gh_user:
        result = subprocess.run(
            ["gh", "auth", "switch", "--user", p.gh_user],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            typer.echo(f"Switched gh account to {green_text(p.gh_user)}")
        else:
            typer.echo(
                f"{yellow_text('warning')}: failed to switch gh account to {p.gh_user}",
                err=True,
            )
            if result.stderr.strip():
                typer.echo(f"  {dim(result.stderr.strip())}", err=True)


@id_app.command("add")
def id_add(
    profile: str = typer.Argument(help="Profile name"),
    name: str = typer.Option(..., "--name", "-n", help="Git user.name"),
    email: str = typer.Option(..., "--email", "-e", help="Git user.email"),
    gh_user: Optional[str] = typer.Option(
        None, "--gh-user", help="GitHub CLI username"
    ),
) -> None:
    """Add a new profile."""
    profiles = _load_profiles()
    if profile in profiles:
        typer.echo(
            f"Profile '{profile}' already exists. Use 'rgit id remove' first.",
            err=True,
        )
        raise typer.Exit(1)

    profiles[profile] = _Profile(name=name, email=email, gh_user=gh_user)
    _save_profiles(profiles)
    desc = f"{name} <{email}>"
    if gh_user:
        desc += f" (gh: {gh_user})"
    typer.echo(f"Added profile {green_text(profile)}: {desc}")


@id_app.command("remove")
def id_remove(
    profile: str = typer.Argument(help="Profile name to remove"),
) -> None:
    """Remove a profile."""
    profiles = _load_profiles()
    if profile not in profiles:
        typer.echo(f"error: profile '{profile}' not found", err=True)
        raise typer.Exit(1)

    del profiles[profile]
    _save_profiles(profiles)
    typer.echo(f"Removed profile {yellow_text(profile)}")


app.add_typer(id_app, name="id")

if __name__ == "__main__":
    app()
