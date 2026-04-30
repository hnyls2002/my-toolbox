#!/bin/bash
# Install rust toolchain for sglang python build deps (e.g. sglang.srt.grpc._core).
#
# Why /host_home/{rustup,cargo} instead of $HOME?
#   Inside the docker container, /root/.rustup/{toolchains,tmp} can land on
#   different overlayfs layers, causing rustup's rename-based install to fail
#   with "Invalid cross-device link (os error 18)". /host_home is a single
#   bind mount, so renames stay on one filesystem.
#
# Why persist into .profile, not .bashrc?
#   debian's .bashrc returns early in non-interactive shells, so `bash -lc`
#   would not pick up the export. .profile runs for every login shell and
#   itself sources .bashrc, so a single export there propagates everywhere.

set -e

export RUSTUP_HOME=/host_home/rustup
export CARGO_HOME=/host_home/cargo
export PATH="$CARGO_HOME/bin:$PATH"

if [ ! -x "$CARGO_HOME/bin/cargo" ]; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable --no-modify-path
fi

RUST_BLOCK_MARKER='# >>> rust toolchain (host_home) >>>'
RUST_BLOCK=$(cat <<EOF
$RUST_BLOCK_MARKER
export RUSTUP_HOME=/host_home/rustup
export CARGO_HOME=/host_home/cargo
[ -f "\$CARGO_HOME/env" ] && . "\$CARGO_HOME/env"
# <<< rust toolchain (host_home) <<<
EOF
)
for rc in /root/.profile /root/.zshrc; do
    [ -f "$rc" ] || touch "$rc"
    grep -qF "$RUST_BLOCK_MARKER" "$rc" || printf '\n%s\n' "$RUST_BLOCK" >> "$rc"
done

# Drop any stale `. "$HOME/.cargo/env"` lines left by previous rustup
# --modify-path runs (the file no longer exists at that path).
for rc in /root/.profile /root/.bashrc; do
    [ -f "$rc" ] && sed -i 's|^\. "\$HOME/.cargo/env"$|# &  # disabled: relocated to /host_home/cargo/env|' "$rc"
done

echo "Rust installed: $($CARGO_HOME/bin/rustc --version)"
