"""CLI entry point for gpt-kit.

gpt-kit          launch the history TUI (drives your logged-in Chrome)
gpt-kit check    verify Chrome automation + ChatGPT login are ready
"""

from __future__ import annotations

import argparse

from my_toolbox.gpt_kit.browser import BrowserError, check
from my_toolbox.gpt_kit.history import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gpt-kit",
        description="Browse and batch-delete your ChatGPT history via Chrome.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="Verify Chrome automation + ChatGPT login are ready.")
    args = parser.parse_args()

    if args.command == "check":
        try:
            for line in check():
                print(line)
        except BrowserError as exc:
            print(f"NOT READY: {exc}")
            raise SystemExit(1)
        return

    run()


if __name__ == "__main__":
    main()
