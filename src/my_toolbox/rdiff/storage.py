"""Storage layout for rdiff.

All generated HTML files live under `RDIFF_HOME` (default `~/.rdiff/html/`).
Users can override individual outputs with `--out`; those are not managed
here and won't show up in `rdiff list` / `prune`.
"""

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def rdiff_home() -> Path:
    override = os.environ.get("RDIFF_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".rdiff"


def html_dir() -> Path:
    d = rdiff_home() / "html"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize(tok: str) -> str:
    """Turn an arbitrary git rev or name into a filename-friendly fragment."""
    tok = tok.strip()
    # Keep short sha; trim the rest.
    tok = tok[:16]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", tok).strip("-") or "rev"


def derive_name(
    *,
    prs: Optional[List[int]] = None,
    base: Optional[str] = None,
    head: Optional[str] = None,
    three_dot: bool = False,
    timestamp: Optional[str] = None,
) -> str:
    """Build a human-readable filename stem (without .html) for this run."""
    ts = timestamp or time.strftime("%Y%m%d-%H%M%S")
    if prs:
        return f"prs-{'-'.join(str(n) for n in prs)}-{ts}"
    sep = "..." if three_dot else ".."
    return f"{_sanitize(base or 'base')}{sep.replace('.', '_')}{_sanitize(head or 'HEAD')}-{ts}"


def output_path(stem: str) -> Path:
    return html_dir() / f"{stem}.html"


@dataclass
class StoredHtml:
    path: Path
    size: int
    mtime: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.mtime


def list_html() -> List[StoredHtml]:
    entries: List[StoredHtml] = []
    for p in sorted(html_dir().glob("*.html")):
        try:
            st = p.stat()
            entries.append(StoredHtml(path=p, size=st.st_size, mtime=st.st_mtime))
        except OSError:
            continue
    entries.sort(key=lambda e: e.mtime, reverse=True)
    return entries


_AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_age(spec: str) -> float:
    """Parse `7d`, `2h`, `30m`, `3600s`, `1w` into seconds."""
    m = re.fullmatch(r"(\d+)([smhdw])", spec.strip())
    if not m:
        raise ValueError(f"Invalid age spec: {spec!r}. Use e.g. 7d, 24h, 30m.")
    n = int(m.group(1))
    return n * _AGE_UNITS[m.group(2)]


def format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"
