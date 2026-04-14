"""rdiff - review-friendly git diff as HTML.

Subcommands:

    rdiff gen --name NAME <ref>                # base..HEAD, all files
    rdiff gen --name NAME <ref>..<head>        # git-style two-dot
    rdiff gen --name NAME <ref>...<head>       # git-style three-dot
    rdiff gen --name NAME <ref> -- path1 path2 # limit to paths
    rdiff gen --name NAME --prs 22213,21875,22651   # 0-noise accumulation
    rdiff gen --name NAME --prs 22651 --base <sha>  # explicit base

    rdiff list                             # show generated HTMLs under ~/.rdiff/html
    rdiff prune                            # interactive: HTMLs + saga + accum worktrees
    rdiff prune --age 7d -y                # scripted: delete items older than 7d

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
    StoredWorktree,
    delete_worktree,
    format_age,
    format_size,
    html_dir,
    list_accum_worktrees,
    list_html,
    list_saga_worktrees,
    output_path,
    parse_age,
    rdiff_home,
)
from my_toolbox.rdiff.util import run as _run
from my_toolbox.ui import cyan_text, dim, green_text, red_text, yellow_text

app = typer.Typer(
    help="Review-friendly git diff as interactive HTML.",
    add_completion=False,
    no_args_is_help=True,
)


# --- helpers ---


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
    """Return path args. Typer strips the `--` sentinel, so any extra
    positional arg after the revision spec is treated as a path filter.

    A mistyped ref like `rdiff gen -n foo HEAD main` would silently turn
    `main` into a path filter — there's no reliable way to distinguish it
    from a legitimate path here. Document the `<ref> -- <paths>` form in
    the help text and trust the user.
    """
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
    base: str,
    head: str,
    three_dot: bool,
    paths: List[str],
    cwd: Path,
    context: int = 3,
) -> str:
    sep = "..." if three_dot else ".."
    spec = f"{base}{sep}{head}"
    cmd = ["git", "diff", f"-U{context}", spec]
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
    context: int = typer.Option(
        3,
        "--context",
        "-U",
        min=0,
        help="Unified diff context lines around each change (git diff -U<N>). "
        "Larger values collapse neighboring hunks into one; smaller shows tighter "
        "unchanged sections. GitHub's default is 3.",
    ),
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
            pr_numbers, base, paths, root, repo=repo, context=context
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

    diff_text = _git_diff(
        base_in, head_resolved, three_dot, paths, root, context=context
    )
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


def _format_item_row(i: int, item: "object") -> str:
    """Format a PruneItem for the list display (3 kinds: html, saga, accum)."""
    if isinstance(item, StoredHtml):
        return (
            f"  [{i:>2}] html   "
            f"{format_age(item.age_seconds):>5}  "
            f"{format_size(item.size):>9}  {item.path.name}"
        )
    assert isinstance(item, StoredWorktree)
    label = "saga  " if item.kind == "saga" else "accum "
    return (
        f"  [{i:>2}] {label} "
        f"{format_age(item.age_seconds):>5}             "
        f"{item.path}  ({item.branch})"
    )


def _parse_selection(raw: str, n: int) -> Optional[List[int]]:
    """Parse '1,3,5' / '1-3' / 'all' / 'none' / '' into 0-based indices.

    Returns None on empty (=cancel). Raises ValueError on malformed input.
    """
    s = raw.strip().lower()
    if s in ("", "none", "n", "q"):
        return None
    if s in ("all", "a", "*"):
        return list(range(n))
    selected: set[int] = set()
    for part in s.replace(",", " ").split():
        if "-" in part:
            lo, hi = part.split("-", 1)
            for k in range(int(lo), int(hi) + 1):
                if 1 <= k <= n:
                    selected.add(k - 1)
        else:
            k = int(part)
            if 1 <= k <= n:
                selected.add(k - 1)
    return sorted(selected)


def _delete_item(item) -> Tuple[bool, str]:
    if isinstance(item, StoredHtml):
        try:
            item.path.unlink()
            return True, "ok"
        except OSError as err:
            return False, str(err)
    assert isinstance(item, StoredWorktree)
    return delete_worktree(item)


@app.command("prune")
def prune(
    age: Optional[str] = typer.Option(
        None, "--age", help="Pre-select items older than this age (e.g. 7d, 24h, 30m)."
    ),
    keep: Optional[int] = typer.Option(
        None, "--keep", help="Pre-select all but the N most recent items per category."
    ),
    all_flag: bool = typer.Option(False, "--all", help="Pre-select everything."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    html: bool = typer.Option(True, "--html/--no-html", help="Include HTMLs."),
    worktree: bool = typer.Option(
        True, "--worktree/--no-worktree", help="Include saga + accum worktrees."
    ),
):
    """Prune HTMLs + saga worktrees + stale rdiff-accum-* worktrees.

    Interactive selection when no filter (--age/--keep/--all) is passed. Filter
    flags pre-select items and prompt for confirmation (or -y to auto-delete).
    """
    htmls = list_html() if html else []

    saga_wts: List[StoredWorktree] = []
    accum_wts: List[StoredWorktree] = []
    if worktree:
        saga_wts = list_saga_worktrees()
        # Scan the current repo for accum worktrees too, if we're inside one.
        try:
            cwd = Path.cwd()
            r = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            repo_root = Path(r.stdout.strip())
            accum_wts = list_accum_worktrees(repo_root)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    all_items: List = [*htmls, *saga_wts, *accum_wts]
    if not all_items:
        typer.echo(dim("Nothing to prune."))
        return

    # Display
    typer.echo(cyan_text(f"rdiff home: {rdiff_home()}"))
    for i, item in enumerate(all_items, start=1):
        typer.echo(_format_item_row(i, item))

    # Decide which indices are pre-selected (--age/--keep/--all) or go interactive.
    filter_modes = [bool(all_flag), age is not None, keep is not None]
    if sum(filter_modes) > 1:
        typer.echo(red_text("--all / --age / --keep are mutually exclusive."), err=True)
        raise typer.Exit(2)

    selected: List[int]
    if all_flag:
        selected = list(range(len(all_items)))
    elif age is not None:
        try:
            limit = parse_age(age)
        except ValueError as e:
            typer.echo(red_text(str(e)), err=True)
            raise typer.Exit(2)
        selected = [i for i, it in enumerate(all_items) if _item_age(it) > limit]
    elif keep is not None:
        if keep < 0:
            typer.echo(red_text("--keep must be >= 0"), err=True)
            raise typer.Exit(2)
        # Per-category newest-first; keep first N of each.
        selected = []
        for bucket in (htmls, saga_wts, accum_wts):
            # bucket is already sorted newest-first in storage.py
            for it in bucket[keep:]:
                selected.append(all_items.index(it))
        selected.sort()
    else:
        # Interactive
        typer.echo(
            dim(
                "Select (e.g. '1,3,5' or '1-4' or 'all' or 'none', " "empty = cancel): "
            ),
            nl=False,
        )
        try:
            raw = input()
        except EOFError:
            raw = ""
        try:
            parsed = _parse_selection(raw, len(all_items))
        except ValueError:
            typer.echo(red_text(f"Invalid selection: {raw!r}"), err=True)
            raise typer.Exit(2)
        if parsed is None:
            typer.echo(dim("Cancelled."))
            return
        selected = parsed

    if not selected:
        typer.echo(green_text("Nothing selected."))
        return

    to_delete = [all_items[i] for i in selected]
    typer.echo(yellow_text(f"Will delete {len(to_delete)} item(s):"))
    for i, item in zip(selected, to_delete):
        typer.echo(_format_item_row(i + 1, item))

    if not yes:
        try:
            confirm = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm not in ("y", "yes"):
            typer.echo(dim("Cancelled."))
            return

    ok_count = 0
    for item in to_delete:
        ok, msg = _delete_item(item)
        if ok:
            ok_count += 1
        else:
            label = item.path.name if isinstance(item, StoredHtml) else str(item.path)
            typer.echo(red_text(f"Failed to delete {label}: {msg}"), err=True)
    typer.echo(green_text(f"Deleted {ok_count}/{len(to_delete)} item(s)."))


def _item_age(item) -> float:
    return item.age_seconds


# --- info / home path ---


@app.command("home")
def home():
    """Print the rdiff storage root (set RDIFF_HOME to override)."""
    typer.echo(str(rdiff_home()))


if __name__ == "__main__":
    app()
