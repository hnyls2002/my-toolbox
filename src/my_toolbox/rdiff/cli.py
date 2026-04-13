"""rdiff - review-friendly git diff as HTML.

Generates a diff2html HTML of BASE..HEAD (or any revision range), injects a
review overlay (per-file Viewed checkbox, per-hunk mark button, clickable
editor links), and opens it in the browser.

Usage:
    rdiff <base>                       # base..HEAD, all files
    rdiff <base>..<head>               # git-style two-dot
    rdiff <base>...<head>              # git-style three-dot (merge-base..head)
    rdiff <base> -- path1 path2        # limit to paths
    rdiff <base>..<head> -- 'src/**/*.py'

If you need A..B, use the range form explicitly. The trailing
`-- <paths>` list is passed directly to `git diff`.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

import typer

from my_toolbox.rdiff.injector import inject
from my_toolbox.ui import cyan_text, dim, green_text, red_text

app = typer.Typer(
    help="Review-friendly git diff as interactive HTML.",
    add_completion=False,
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": False,
    },
)


def _run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def _git_toplevel(cwd: Optional[Path] = None) -> Path:
    r = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if r.returncode != 0:
        typer.echo(red_text("Not inside a git repository."), err=True)
        raise typer.Exit(2)
    return Path(r.stdout.strip())


def _parse_ref_spec(ref: str) -> Tuple[str, str, bool]:
    """Parse `<base>`, `<base>..<head>`, or `<base>...<head>`.

    Returns (base, head, three_dot). A bare `<base>` implies head=HEAD.
    """
    if "..." in ref:
        base, head_from_ref = ref.split("...", 1)
        return base or "HEAD", head_from_ref or "HEAD", True
    if ".." in ref:
        base, head_from_ref = ref.split("..", 1)
        return base or "HEAD", head_from_ref or "HEAD", False
    return ref, "HEAD", False


def _split_paths(extra_args: List[str]) -> List[str]:
    """Pull paths after the first `--` sentinel. If no `--`, treat all extras as paths."""
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


def _default_out_path() -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    return Path(tempfile.gettempdir()) / f"rdiff-{ts}.html"


@app.command()
def main(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="Revision spec: `A` (= A..HEAD), `A..B`, or `A...B` (three-dot).",
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Output HTML file (default: /tmp/rdiff-<ts>.html)."
    ),
    style: str = typer.Option("side", "--style", help="side or line."),
    title: Optional[str] = typer.Option(
        None, "--title", help="Page title (default: derived from rev spec)."
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the generated HTML in the browser."
    ),
    repo_root: Optional[Path] = typer.Option(
        None,
        "--repo-root",
        help="Repo root for editor links (default: `git rev-parse --show-toplevel`).",
    ),
):
    """Generate an interactive review HTML for a git diff."""
    _which_or_die(
        "diff2html",
        hint="Install with: npm install -g diff2html-cli",
    )
    _which_or_die("git")

    cwd = Path.cwd()
    root = repo_root.resolve() if repo_root else _git_toplevel(cwd)

    # Parse ref spec.
    base, head_resolved, three_dot = _parse_ref_spec(ref)
    extra = list(ctx.args)
    paths = _split_paths(extra)

    # Validate revs.
    base_short = _resolve_rev(base, root)
    head_short = _resolve_rev(head_resolved, root)

    sep = "..." if three_dot else ".."
    spec_label = f"{base_short}{sep}{head_short}"
    display_title = title or f"rdiff {spec_label}"

    typer.echo(cyan_text(f"Diff: {spec_label}"))
    if paths:
        typer.echo(dim(f"Paths: {' '.join(paths)}"))
    typer.echo(dim(f"Repo: {root}"))

    diff_text = _git_diff(base, head_resolved, three_dot, paths, root)
    if not diff_text.strip():
        typer.echo(red_text("Empty diff - nothing to render."), err=True)
        raise typer.Exit(1)

    out_path = (out or _default_out_path()).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _run_diff2html(diff_text, out_path, style, display_title)
    inject(out_path, str(root))

    typer.echo(green_text(f"Wrote: {out_path}"))
    typer.echo(dim(f"URL:   file://{out_path}"))

    if open_browser:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(out_path)])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(out_path)])
        else:
            typer.echo(dim("Auto-open not supported on this platform."))


if __name__ == "__main__":
    app()
