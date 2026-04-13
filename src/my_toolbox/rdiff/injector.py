"""Inject review UI (per-file viewed, per-hunk mark, clickable editor links)
into a diff2html-generated HTML file."""

import re
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"


def _load_assets(repo_root: str) -> str:
    css = (ASSETS / "review.css").read_text()
    js = (ASSETS / "review.js").read_text().replace("__REPO_ROOT__", repo_root)
    return (
        '<style id="d2h-review-style">\n'
        + css
        + "\n</style>\n<script>\n"
        + js
        + "\n</script>\n"
    )


def inject(html_path: Path, repo_root: str) -> None:
    """Inject review UI into html_path in-place.

    Removes any previous injection to keep reruns idempotent.
    """
    html = html_path.read_text()

    # Remove older injection blocks so we don't double-inject on rerun.
    html = re.sub(
        r'<style id="d2h-review-style">[\s\S]*?</script>\s*',
        "",
        html,
    )

    injected = _load_assets(repo_root)
    new = html.replace("</body>", injected + "</body>", 1)
    html_path.write_text(new)
