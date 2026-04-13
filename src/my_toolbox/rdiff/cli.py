"""rdiff - review-friendly git diff as HTML.

Subcommands:

    rdiff gen --name NAME <ref>                # base..HEAD, all files
    rdiff gen --name NAME <ref>..<head>        # git-style two-dot
    rdiff gen --name NAME <ref>...<head>       # git-style three-dot
    rdiff gen --name NAME <ref> -- path1 path2 # limit to paths
    rdiff gen --name NAME --prs 22213,21875,22651   # 0-noise accumulation
    rdiff gen --name NAME --prs 22651 --base <sha>  # explicit base

    rdiff list                             # show generated HTMLs under ~/.rdiff/html
    rdiff prune [--age 7d|--keep N|--all]  # delete old HTMLs
    rdiff clean                            # remove stale rdiff-accum-* worktrees

`--name NAME` writes ~/.rdiff/html/NAME.html and is managed by list/prune.
Same name -> same file -> browser localStorage review state persists.

`--out PATH` is an escape hatch for writing anywhere else; those files are
NOT managed by list/prune. --name and --out are mutually exclusive.

Set RDIFF_HOME to relocate the storage root (default ~/.rdiff).
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import typer

from my_toolbox.rdiff.accumulator import build_accumulation_diff
from my_toolbox.rdiff.injector import inject
from my_toolbox.rdiff.storage import (
    StoredHtml,
    format_age,
    format_size,
    html_dir,
    list_html,
    output_path,
    parse_age,
    rdiff_home,
)
from my_toolbox.ui import cyan_text, dim, green_text, red_text, yellow_text

app = typer.Typer(
    help="Review-friendly git diff as interactive HTML.",
    add_completion=False,
    no_args_is_help=True,
)


# --- helpers ---


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def _git_toplevel(cwd: Optional[Path] = None) -> Path:
    r = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if r.returncode != 0:
        typer.echo(red_text("Not inside a git repository."), err=True)
        raise typer.Exit(2)
    return Path(r.stdout.strip())


def _parse_ref_spec(ref: str) -> Tuple[str, str, bool]:
    if "..." in ref:
        base, head_from_ref = ref.split("...", 1)
        return base or "HEAD", head_from_ref or "HEAD", True
    if ".." in ref:
        base, head_from_ref = ref.split("..", 1)
        return base or "HEAD", head_from_ref or "HEAD", False
    return ref, "HEAD", False


def _split_paths(extra_args: List[str]) -> List[str]:
    if "--" in extra_args:
        i = extra_args.index("--")
        return extra_args[i + 1 :]
    return list(extra_args)


def _which_or_die(bin_name: str, hint: str = "") -> None:
    if shutil.which(bin_name) is None:
        typer.echo(red_text(f"Required binary not found: {bin_name}"), err=True)
        if hint:
            typer.echo(dim(hint), err=True)
        raise typer.Exit(2)


def _resolve_rev(rev: str, cwd: Path) -> str:
    r = _run(["git", "rev-parse", "--short", rev], cwd=cwd)
    if r.returncode != 0:
        typer.echo(red_text(f"Cannot resolve revision: {rev}"), err=True)
        typer.echo(dim(r.stderr.strip()), err=True)
        raise typer.Exit(2)
    return r.stdout.strip()


def _git_diff(
    base: str, head: str, three_dot: bool, paths: List[str], cwd: Path
) -> str:
    sep = "..." if three_dot else ".."
    spec = f"{base}{sep}{head}"
    cmd = ["git", "diff", spec]
    if paths:
        cmd.append("--")
        cmd.extend(paths)
    r = _run(cmd, cwd=cwd)
    if r.returncode != 0:
        typer.echo(red_text("git diff failed:"), err=True)
        typer.echo(r.stderr, err=True)
        raise typer.Exit(r.returncode)
    return r.stdout


def _run_diff2html(diff_text: str, out_path: Path, style: str, title: str) -> None:
    cmd = [
        "diff2html",
        "-i",
        "stdin",
        "-s",
        style,
        "--su",
        "closed",
        "-t",
        title,
        "--diffMaxChanges",
        "10000",
        "--diffMaxLineLength",
        "5000",
        "-F",
        str(out_path),
    ]
    r = subprocess.run(cmd, input=diff_text, text=True, capture_output=True)
    if r.returncode != 0:
        typer.echo(red_text("diff2html failed:"), err=True)
        typer.echo(r.stderr, err=True)
        raise typer.Exit(r.returncode)


def _open(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", str(path)])
    else:
        typer.echo(dim("Auto-open not supported on this platform."))


def _render(
    diff_text: str,
    out_path: Path,
    style: str,
    title: str,
    repo_root: Path,
    open_browser: bool,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run_diff2html(diff_text, out_path, style, title)
    inject(out_path, str(repo_root))
    typer.echo(green_text(f"Wrote: {out_path}"))
    typer.echo(dim(f"URL:   file://{out_path}"))
    if open_browser:
        _open(out_path)


# --- gen ---


@app.command(
    "gen", context_settings={"allow_extra_args": True, "ignore_unknown_options": False}
)
def gen(
    ctx: typer.Context,
    ref: Optional[str] = typer.Argument(
        None,
        help="Revision spec: `A` (= A..HEAD), `A..B`, or `A...B` (three-dot). "
        "Omit when using --prs.",
    ),
    prs: Optional[str] = typer.Option(
        None,
        "--prs",
        help="Comma-separated PR numbers to combine (accumulation mode).",
    ),
    base: Optional[str] = typer.Option(
        None, "--base", help="Base rev for accumulation mode (default: auto)."
    ),
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="owner/name for `gh` queries (default: from `gh repo view`).",
    ),
    name: Optional[str] = typer.Option(
        None,
        "--name",
        "-n",
        help="Name under ~/.rdiff/html/<name>.html. Managed by list/prune. "
        "Same name -> same file, so review state (localStorage) persists.",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Escape hatch: arbitrary output path. NOT managed by list/prune. "
        "Mutually exclusive with --name.",
    ),
    style: str = typer.Option("side", "--style", help="side or line."),
    title: Optional[str] = typer.Option(None, "--title"),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the generated HTML in the browser."
    ),
    repo_root: Optional[Path] = typer.Option(
        None,
        "--repo-root",
        help="Repo root for editor links (default: `git rev-parse --show-toplevel`).",
    ),
):
    """Generate a review HTML."""
    _which_or_die("diff2html", hint="Install with: npm install -g diff2html-cli")
    _which_or_die("git")

    cwd = Path.cwd()
    root = repo_root.resolve() if repo_root else _git_toplevel(cwd)
    extra = list(ctx.args)
    paths = _split_paths(extra)

    # Resolve output path: exactly one of --name / --out required.
    if name and out:
        typer.echo(red_text("--name and --out are mutually exclusive."), err=True)
        raise typer.Exit(2)
    if not name and not out:
        typer.echo(
            red_text(
                "Must specify --name <NAME> (managed) or --out <PATH> (unmanaged)."
            ),
            err=True,
        )
        raise typer.Exit(2)
    if name:
        try:
            out_path = output_path(name)
        except ValueError as e:
            typer.echo(red_text(str(e)), err=True)
            raise typer.Exit(2)
    else:
        out_path = out.resolve()

    if prs:
        if ref is not None:
            typer.echo(red_text("Cannot combine a revision spec with --prs."), err=True)
            raise typer.Exit(2)
        try:
            pr_numbers = [int(x.strip()) for x in prs.split(",") if x.strip()]
        except ValueError:
            typer.echo(red_text(f"Invalid --prs value: {prs}"), err=True)
            raise typer.Exit(2)
        if not pr_numbers:
            typer.echo(red_text("--prs is empty."), err=True)
            raise typer.Exit(2)

        typer.echo(cyan_text(f"Accumulating PRs: {pr_numbers}"))
        diff_text, base_sha = build_accumulation_diff(
            pr_numbers, base, paths, root, repo=repo
        )
        if not diff_text.strip():
            typer.echo(red_text("Empty accumulated diff."), err=True)
            raise typer.Exit(1)

        display_title = (
            title
            or f"rdiff prs={','.join(str(n) for n in pr_numbers)} @ {base_sha[:12]}"
        )
        _render(diff_text, out_path, style, display_title, root, open_browser)
        return

    if ref is None:
        typer.echo(red_text("Missing revision spec. See `rdiff gen --help`."), err=True)
        raise typer.Exit(2)

    base_in, head_resolved, three_dot = _parse_ref_spec(ref)
    base_short = _resolve_rev(base_in, root)
    head_short = _resolve_rev(head_resolved, root)
    sep = "..." if three_dot else ".."
    spec_label = f"{base_short}{sep}{head_short}"
    display_title = title or f"rdiff {spec_label}"

    typer.echo(cyan_text(f"Diff: {spec_label}"))
    if paths:
        typer.echo(dim(f"Paths: {' '.join(paths)}"))
    typer.echo(dim(f"Repo: {root}"))

    diff_text = _git_diff(base_in, head_resolved, three_dot, paths, root)
    if not diff_text.strip():
        typer.echo(red_text("Empty diff - nothing to render."), err=True)
        raise typer.Exit(1)

    _render(diff_text, out_path, style, display_title, root, open_browser)


# --- list ---


@app.command("list")
def list_cmd(
    long: bool = typer.Option(False, "--long", "-l", help="Show full paths."),
):
    """List generated HTMLs in ~/.rdiff/html/."""
    entries = list_html()
    if not entries:
        typer.echo(dim(f"No HTMLs under {html_dir()}"))
        return
    total = sum(e.size for e in entries)
    typer.echo(
        cyan_text(f"{len(entries)} file(s) under {html_dir()}  ({format_size(total)})")
    )
    for e in entries:
        age = format_age(e.age_seconds)
        size = format_size(e.size)
        name = str(e.path) if long else e.path.name
        typer.echo(f"  {age:>5}  {size:>9}  {name}")


# --- prune ---


@app.command("prune")
def prune(
    age: Optional[str] = typer.Option(
        None, "--age", help="Delete files older than this age (e.g. 7d, 24h, 30m)."
    ),
    keep: Optional[int] = typer.Option(
        None, "--keep", help="Keep only the N most recent files."
    ),
    all_flag: bool = typer.Option(False, "--all", help="Delete everything."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
):
    """Prune generated HTMLs. Dry-run unless -y is passed."""
    entries = list_html()
    if not entries:
        typer.echo(dim(f"No HTMLs under {html_dir()}"))
        return

    # Determine the set to delete.
    to_delete: List[StoredHtml]
    if all_flag:
        to_delete = list(entries)
    elif age is not None:
        try:
            limit = parse_age(age)
        except ValueError as e:
            typer.echo(red_text(str(e)), err=True)
            raise typer.Exit(2)
        to_delete = [e for e in entries if e.age_seconds > limit]
    elif keep is not None:
        if keep < 0:
            typer.echo(red_text("--keep must be >= 0"), err=True)
            raise typer.Exit(2)
        to_delete = entries[keep:]  # entries sorted newest-first
    else:
        typer.echo(red_text("Specify one of --age, --keep, --all."), err=True)
        raise typer.Exit(2)

    if not to_delete:
        typer.echo(green_text("Nothing to prune."))
        return

    total = sum(e.size for e in to_delete)
    typer.echo(
        yellow_text(f"Will delete {len(to_delete)} file(s), {format_size(total)}:")
    )
    for e in to_delete:
        typer.echo(
            f"  {format_age(e.age_seconds):>5}  {format_size(e.size):>9}  {e.path.name}"
        )

    if not yes:
        typer.echo(dim("(dry-run; pass -y to actually delete)"))
        return

    for e in to_delete:
        try:
            e.path.unlink()
        except OSError as err:
            typer.echo(red_text(f"Failed to delete {e.path}: {err}"), err=True)
    typer.echo(green_text(f"Deleted {len(to_delete)} file(s)."))


# --- clean (stale worktrees / branches) ---


@app.command("clean")
def clean(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
):
    """Remove stale rdiff-accum-* worktrees and branches from the current repo.

    Run this from inside the repo where you've been using `rdiff gen --prs`.
    Safe to run when there are no stale entries.
    """
    _which_or_die("git")
    cwd = Path.cwd()
    root = _git_toplevel(cwd)

    r = _run(["git", "worktree", "list", "--porcelain"], cwd=root)
    if r.returncode != 0:
        typer.echo(red_text("git worktree list failed:"), err=True)
        typer.echo(r.stderr, err=True)
        raise typer.Exit(2)

    stale_worktrees = []
    current_wt = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            current_wt = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            branch = line.split("refs/heads/", 1)[-1]
            if branch.startswith("rdiff-accum-") and current_wt:
                stale_worktrees.append((current_wt, branch))

    # Also look for orphan branches (worktree gone but branch still there).
    r = _run(["git", "branch", "--list", "rdiff-accum-*"], cwd=root)
    branch_names = set()
    for line in r.stdout.splitlines():
        name = line.strip().lstrip("*").strip()
        if name:
            branch_names.add(name)
    already = {b for _, b in stale_worktrees}
    orphan_branches = sorted(branch_names - already)

    if not stale_worktrees and not orphan_branches:
        typer.echo(green_text("Clean: no stale rdiff-accum entries."))
        return

    typer.echo(yellow_text("Will remove:"))
    for wt, br in stale_worktrees:
        typer.echo(f"  worktree {wt} (branch {br})")
    for br in orphan_branches:
        typer.echo(f"  orphan branch {br}")

    if not yes:
        typer.echo(dim("(dry-run; pass -y to actually delete)"))
        return

    for wt, _ in stale_worktrees:
        _run(["git", "worktree", "remove", "--force", wt], cwd=root)
    for wt, br in stale_worktrees:
        _run(["git", "branch", "-D", br], cwd=root)
    for br in orphan_branches:
        _run(["git", "branch", "-D", br], cwd=root)

    typer.echo(
        green_text(
            f"Removed {len(stale_worktrees)} worktree(s), "
            f"{len(stale_worktrees) + len(orphan_branches)} branch(es)."
        )
    )


# --- info / home path ---


@app.command("home")
def home():
    """Print the rdiff storage root (set RDIFF_HOME to override)."""
    typer.echo(str(rdiff_home()))


if __name__ == "__main__":
    app()
