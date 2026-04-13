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
from typing import List


def rdiff_home() -> Path:
    override = os.environ.get("RDIFF_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".rdiff"


def html_dir() -> Path:
    d = rdiff_home() / "html"
    d.mkdir(parents=True, exist_ok=True)
    return d


_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_name(name: str) -> str:
    """Validate a user-supplied --name. Raises ValueError if malformed.

    Allowed chars: letters, digits, `.`, `_`, `-`. No path separators.
    """
    name = name.strip()
    if not name:
        raise ValueError("--name cannot be empty")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"--name must match [A-Za-z0-9._-]+ (got {name!r})")
    if name.endswith(".html"):
        name = name[:-5]
    return name


def output_path(name: str) -> Path:
    """Compute the managed output path for a --name."""
    return html_dir() / f"{validate_name(name)}.html"


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
