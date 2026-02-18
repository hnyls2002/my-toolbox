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
from datetime import datetime
from pathlib import Path

from my_toolbox.ui import (
    bold,
    cyan_text,
    dim,
    green_text,
    red_text,
    section_header,
    warn_banner,
)

RSYNC_BASE = [
    "rsync",
    "-a",
    "--info=progress2",
    "--human-readable",
    "--partial",
    "--append-verify",
    "--no-owner",
    "--no-group",
    "--no-perms",
    "--no-compress",
]

BULK_SYNC_TMP = Path("/tmp/bulk_sync")


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


def _run_rsync(cmd: list[str], label: str = "") -> int:
    header = f"Syncing {label}" if label else "Syncing"
    print(f"\n{section_header(header)}")
    print(f"  {dim('$ ' + shlex.join(cmd))}\n")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for line in proc.stdout:
        sys.stdout.write(f"  {dim(line.rstrip())}\n")
        sys.stdout.flush()
    return proc.wait()


def _prepare_tmp():
    """Clean up stale tmp from previous interrupted runs, then create fresh."""
    if BULK_SYNC_TMP.exists():
        shutil.rmtree(BULK_SYNC_TMP)
        print(f"{green_text('✓')} Cleaned up stale tmp: {BULK_SYNC_TMP}")
    BULK_SYNC_TMP.mkdir(parents=True)
    print(f"{green_text('✓')} Created tmp: {BULK_SYNC_TMP}")


def _cleanup_tmp():
    if BULK_SYNC_TMP.exists():
        shutil.rmtree(BULK_SYNC_TMP, ignore_errors=True)
        print(f"\n{green_text('✓')} Cleaned up tmp: {BULK_SYNC_TMP}")


def _print_plan(
    src: str,
    dst: str,
    src_container: str | None,
    dst_container: str | None,
    relay: bool,
):
    print(section_header("Bulk Sync"))
    print(f"  Source:  {bold(src)}")
    print(f"  Dest:    {bold(dst)}")

    if src_container:
        host, _ = _split_remote(src)
        print(f"  Docker:  {cyan_text(src_container)} (src @ {cyan_text(host)})")
    if dst_container:
        host, _ = _split_remote(dst)
        print(f"  Docker:  {cyan_text(dst_container)} (dst @ {cyan_text(host)})")

    if relay:
        print(f"\n{warn_banner('Relaying via local temp (both sides remote)')}")
        print(f"  Tmp:     {dim(str(BULK_SYNC_TMP))}")
        src_host = _split_remote(src)[0]
        dst_host = _split_remote(dst)[0]
        print(f"  Step 1:  {cyan_text(src_host)} -> local tmp")
        print(f"  Step 2:  local tmp -> {cyan_text(dst_host)}")


def _done(src: str, dst: str):
    now = dim(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print(f"{green_text('✓')} Done  {now}  {src} -> {dst}")


def _fail(rc: int, step: str = ""):
    label = f"  {step}" if step else ""
    print(f"\n{red_text('✗')} Failed (exit {rc}){label}", file=sys.stderr)


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

    relay = src_remote and dst_remote
    _print_plan(args.src, args.dst, args.src_container, args.dst_container, relay)
    input(dim("\n  ⏎  Press Enter to continue..."))

    if relay:
        tmpdir = str(BULK_SYNC_TMP)
        _prepare_tmp()
        try:
            rc = _run_rsync(
                _build_rsync_cmd(args.src, tmpdir, src_container=args.src_container),
                label="Step 1 (src -> tmp)",
            )
            if rc != 0:
                _fail(rc, "Step 1 (src -> tmp)")
                sys.exit(rc)

            src_basename = args.src.rsplit("/", 1)[-1] if "/" in args.src else ""
            relay_src = f"{tmpdir}/{src_basename}" if src_basename else f"{tmpdir}/"

            rc = _run_rsync(
                _build_rsync_cmd(relay_src, args.dst, dst_container=args.dst_container),
                label="Step 2 (tmp -> dst)",
            )
            if rc != 0:
                _fail(rc, "Step 2 (tmp -> dst)")
                sys.exit(rc)
        finally:
            _cleanup_tmp()
    else:
        rc = _run_rsync(
            _build_rsync_cmd(
                args.src,
                args.dst,
                src_container=args.src_container,
                dst_container=args.dst_container,
            ),
        )
        if rc != 0:
            _fail(rc)
            sys.exit(rc)

    if not relay:
        print()
    _done(args.src, args.dst)


if __name__ == "__main__":
    main()
