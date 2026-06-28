#!/bin/bash
# Install a worktree from /mirror/common_sync/<name> into the container.
# Symlinks /mirror/common_sync/<name> -> /root/<name>, then pip-installs it
# editable. The installable dir is auto-detected: $SRC/python (the sglang
# layout) if present, otherwise $SRC itself (my-toolbox and other root-level
# packages).
#
# Usage: install_worktree.sh <name>
#   e.g. install_worktree.sh sglang
#        install_worktree.sh sglang-dsv4
#        install_worktree.sh my-toolbox

set -e

# Ensure rust toolchain is on PATH (installed by setup.sh via rustup)
export PATH="$HOME/.cargo/bin:$PATH"

NAME="${1:-sglang}"

SRC="/mirror/common_sync/$NAME"
DST="/root/$NAME"

if [ ! -d "$SRC" ]; then
    echo "Source not found: $SRC" >&2
    exit 1
fi

# Detect the installable subdir: prefer python/ (sglang layout), else the
# repo root when it carries an installable package itself.
if [ -d "$SRC/python" ]; then
    PIP_DIR="$DST/python"
elif [ -f "$SRC/pyproject.toml" ] || [ -f "$SRC/setup.py" ]; then
    PIP_DIR="$DST"
else
    echo "Not an installable worktree (no $SRC/python, and no pyproject.toml/setup.py at $SRC)" >&2
    exit 1
fi

ln -sfn "$SRC" "$DST"
cd "$PIP_DIR" && pip install -e . --config-settings editable_mode=compat

echo "Installed worktree: $NAME (editable dir: ${PIP_DIR#$DST})"
