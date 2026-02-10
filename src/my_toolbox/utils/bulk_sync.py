#!/usr/bin/env python3
"""Bulk sync large files/directories between machines via rsync."""

import argparse
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Bulk sync large files via rsync")
    parser.add_argument("src", help="Source path (e.g. host:/path/to/dir)")
    parser.add_argument("dst", help="Destination path")
    args = parser.parse_args()

    cmd = [
        "rsync",
        "-av",
        "--info=progress2",
        "--human-readable",
        "--stats",
        "--partial",
        "--append-verify",
        "--no-owner",
        "--no-group",
        "--no-perms",
        "--no-compress",
        "-e",
        "ssh -T -o Compression=no",
        args.src,
        args.dst,
    ]

    print(f"Syncing from {args.src} to {args.dst}")
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
