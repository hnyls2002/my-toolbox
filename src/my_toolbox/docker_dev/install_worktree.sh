#!/bin/bash
# Install a sglang worktree from /host_home/common_sync/<name> into the container.
# Symlinks /host_home/common_sync/<name> -> /root/<name>, then pip installs <name>/python.
#
# Usage: install_worktree.sh <name>
#   e.g. install_worktree.sh sglang
#        install_worktree.sh sglang-dsv4

set -e

# Ensure rust toolchain is on PATH (installed by setup.sh via rustup)
export PATH="$HOME/.cargo/bin:$PATH"

NAME="${1:-sglang}"

SRC="/host_home/common_sync/$NAME"
DST="/root/$NAME"

if [ ! -d "$SRC" ]; then
    echo "Source not found: $SRC" >&2
    exit 1
fi
if [ ! -d "$SRC/python" ]; then
    echo "Not a sglang worktree (missing $SRC/python)" >&2
    exit 1
fi

ln -sfn "$SRC" "$DST"
cd "$DST/python" && pip install -e . --config-settings editable_mode=compat

echo "Installed worktree: $NAME"
