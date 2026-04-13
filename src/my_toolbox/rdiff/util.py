"""Shared helpers for rdiff CLI and accumulator."""

import subprocess
from typing import List


def run(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    """Run a subprocess with captured text output, without raising on non-zero."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)
