#!/bin/bash
# Install rust toolchain for sglang python build deps (e.g. sglang.srt.grpc._core).
#
# Why /mirror/toolchains instead of $HOME?
#   Inside the docker container, /root/.rustup/{toolchains,tmp} can land on
#   different overlayfs layers, causing rustup's rename-based install to fail
#   with "Invalid cross-device link (os error 18)". /mirror is a single
#   bind mount, so renames stay on one filesystem. The toolchains/ subdir
#   leaves the mirror root to the synced source tree.
#
# Why export PATH instead of sourcing $CARGO_HOME/env?
#   rustup writes the install-time CARGO_HOME into that file literally, so it
#   goes stale on any move.
#
# Why persist into .profile, not .bashrc?
#   debian's .bashrc returns early in non-interactive shells, so `bash -lc`
#   would not pick up the export. .profile runs for every login shell and
#   itself sources .bashrc, so a single export there propagates everywhere.

set -e

TOOLCHAIN_DIR=/mirror/toolchains
export RUSTUP_HOME="$TOOLCHAIN_DIR/rustup"
export CARGO_HOME="$TOOLCHAIN_DIR/cargo"
export PATH="$CARGO_HOME/bin:$PATH"

# Adopt the flat pre-toolchains layout instead of re-downloading it.
mkdir -p "$TOOLCHAIN_DIR"
for d in rustup cargo; do
    if [ -d "/mirror/$d" ] && [ ! -e "$TOOLCHAIN_DIR/$d" ]; then
        mv "/mirror/$d" "$TOOLCHAIN_DIR/$d"
        echo "Relocated /mirror/$d -> $TOOLCHAIN_DIR/$d"
    fi
done

if [ ! -x "$CARGO_HOME/bin/cargo" ]; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable --no-modify-path
fi

# Rewrite, not append-if-absent: a container holding an older block must pick
# up the new paths instead of keeping a stale one behind the marker.
RUST_BLOCK=$(cat <<EOF
# >>> rust toolchain (mirror) >>>
export RUSTUP_HOME=$RUSTUP_HOME
export CARGO_HOME=$CARGO_HOME
export PATH="\$CARGO_HOME/bin:\$PATH"
# <<< rust toolchain (mirror) <<<
EOF
)
for rc in /root/.profile /root/.zshrc; do
    [ -f "$rc" ] || touch "$rc"
    sed -i '/^# >>> rust toolchain (mirror) >>>$/,/^# <<< rust toolchain (mirror) <<<$/d' "$rc"
    printf '\n%s\n' "$RUST_BLOCK" >> "$rc"
done

# Drop any stale `. "$HOME/.cargo/env"` lines left by previous rustup
# --modify-path runs (the file no longer exists at that path).
for rc in /root/.profile /root/.bashrc; do
    [ -f "$rc" ] && sed -i 's|^\. "\$HOME/.cargo/env"$|# \&  # disabled: relocated to '"$CARGO_HOME"'|' "$rc"
done

echo "Rust installed: $($CARGO_HOME/bin/rustc --version)"
