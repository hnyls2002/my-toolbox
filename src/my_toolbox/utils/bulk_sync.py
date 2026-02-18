#!/usr/bin/env python3
"""Bulk sync large files/directories between machines via rsync.

Transport modes:
  SSH:            bulk-sync host:/path /local
  SSH + Docker:   bulk-sync --src-container CTR host:/path /local
  Both remote:    bulk-sync host1:/path host2:/path  (relays via local temp)
"""

import argparse
import shlex
import shutil
import subprocess
import sys
import tempfile

RSYNC_BASE = [
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
]


def _is_remote(path: str) -> bool:
    return ":" in path


def _split_remote(path: str) -> tuple[str, str]:
    """Split 'host:/path' into (host, path)."""
    host, _, remote_path = path.partition(":")
    return host, remote_path


def _build_rsync_cmd(
    src: str,
    dst: str,
    src_container: str | None = None,
    dst_container: str | None = None,
) -> list[str]:
    """Build a single rsync command for a one-remote-at-most transfer."""
    cmd = list(RSYNC_BASE)

    src_remote = _is_remote(src)
    dst_remote = _is_remote(dst)
    rsh: str | None = None

    if src_container and src_remote:
        host, path = _split_remote(src)
        src = f"{src_container}:{path}"
        rsh = f"ssh -T -o Compression=no {host} docker exec -i"
    elif dst_container and dst_remote:
        host, path = _split_remote(dst)
        dst = f"{dst_container}:{path}"
        rsh = f"ssh -T -o Compression=no {host} docker exec -i"
    elif src_remote or dst_remote:
        rsh = "ssh -T -o Compression=no"

    if rsh:
        cmd += ["-e", rsh]
    cmd += [src, dst]
    return cmd


def _run_rsync(cmd: list[str]) -> int:
    print(f"cmd: {shlex.join(cmd)}")
    return subprocess.call(cmd)


def main():
    parser = argparse.ArgumentParser(description="Bulk sync large files via rsync")
    parser.add_argument("src", help="Source path (host:/path or /local/path)")
    parser.add_argument("dst", help="Destination path (host:/path or /local/path)")
    parser.add_argument(
        "--src-container",
        metavar="CONTAINER",
        help="Docker container on the source host (requires rsync in container)",
    )
    parser.add_argument(
        "--dst-container",
        metavar="CONTAINER",
        help="Docker container on the destination host (requires rsync in container)",
    )
    args = parser.parse_args()

    src_remote = _is_remote(args.src)
    dst_remote = _is_remote(args.dst)

    if args.src_container and not src_remote:
        parser.error("--src-container requires src to be remote (host:/path)")
    if args.dst_container and not dst_remote:
        parser.error("--dst-container requires dst to be remote (host:/path)")

    print(f"Syncing from {args.src} to {args.dst}")

    if src_remote and dst_remote:
        tmpdir = tempfile.mkdtemp(prefix="bulk_sync_")
        print(f"Both sides are remote, relaying via {tmpdir}")
        try:
            rc = _run_rsync(
                _build_rsync_cmd(
                    args.src,
                    tmpdir,
                    src_container=args.src_container,
                )
            )
            if rc != 0:
                print(
                    f"Step 1 (src -> tmp) failed with exit code {rc}", file=sys.stderr
                )
                sys.exit(rc)

            src_basename = args.src.rsplit("/", 1)[-1] if "/" in args.src else ""
            relay_src = f"{tmpdir}/{src_basename}" if src_basename else f"{tmpdir}/"

            rc = _run_rsync(
                _build_rsync_cmd(
                    relay_src,
                    args.dst,
                    dst_container=args.dst_container,
                )
            )
            if rc != 0:
                print(
                    f"Step 2 (tmp -> dst) failed with exit code {rc}", file=sys.stderr
                )
                sys.exit(rc)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        rc = _run_rsync(
            _build_rsync_cmd(
                args.src,
                args.dst,
                src_container=args.src_container,
                dst_container=args.dst_container,
            )
        )
        sys.exit(rc)


if __name__ == "__main__":
    main()
