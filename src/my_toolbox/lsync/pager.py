"""Pager helper for displaying content through less."""

import os
import subprocess
import sys

# -F  quit if one screen
# -R  raw control characters (preserves ANSI colors)
# -X  don't clear screen on exit
DEFAULT_PAGER = "less -FRX"


def page(content: str) -> None:
    """Display *content* through a pager, like ``git log`` does.

    Respects the PAGER environment variable. Falls back to direct
    stdout when not a TTY.
    """
    if not sys.stdout.isatty():
        sys.stdout.write(content)
        return

    pager_cmd = os.environ.get("PAGER", DEFAULT_PAGER)
    try:
        proc = subprocess.Popen(
            pager_cmd,
            shell=True,
            stdin=subprocess.PIPE,
            encoding="utf-8",
        )
        proc.communicate(input=content)
    except (OSError, BrokenPipeError):
        sys.stdout.write(content)
