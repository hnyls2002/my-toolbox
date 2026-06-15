#!/bin/bash
set -e

# apt utils
apt update
apt install -y neovim rsync

# pip utils
pip install nvitop gpustat

# rust toolchain (required by some sglang python build deps)
bash "$(dirname "$0")/install_rust.sh"

# my-toolbox (sglang worktree is installed separately via install_worktree.sh)
ln -sfn /mirror/common_sync/my-toolbox /root/my-toolbox
cd /root/my-toolbox && pip install -e . --config-settings editable_mode=compat

# SYNC_ROOT for rgit / rdev tools inside container
SYNC_ROOT_LINE='export SYNC_ROOT=/mirror/common_sync'
for rc in /root/.bashrc /root/.zshrc; do
    grep -qxF "$SYNC_ROOT_LINE" "$rc" 2>/dev/null || echo "$SYNC_ROOT_LINE" >> "$rc"
done

# optional local HF cache (arg $1 = local dir; empty = keep gcsfuse default).
# Redirects HF_HOME in the shell rc to a devbox-local path, bypassing the
# infra-managed shared gcsfuse cache and its lock/rename pitfalls. Must unset
# HUGGINGFACE_HUB_CACHE / TRANSFORMERS_CACHE too: both are more specific than
# HF_HOME and would otherwise keep pointing the blob cache at gcsfuse.
HF_CACHE_LOCAL="${1:-}"
if [ -n "$HF_CACHE_LOCAL" ]; then
    mkdir -p "$HF_CACHE_LOCAL"
    for rc in /root/.bashrc /root/.zshrc; do
        for line in 'unset HUGGINGFACE_HUB_CACHE TRANSFORMERS_CACHE' "export HF_HOME=$HF_CACHE_LOCAL"; do
            grep -qxF "$line" "$rc" 2>/dev/null || echo "$line" >> "$rc"
        done
    done
fi

# setup tmux (idempotent: devbox-init reruns this script on each acquire)
while IFS= read -r line; do
    grep -qxF "$line" /root/.tmux.conf 2>/dev/null || echo "$line" >> /root/.tmux.conf
done <<'EOF'
set -g mouse on
setw -g mode-keys vi
set -g history-limit 100000
EOF

# setup vimrc
VIM_DIR="/mirror/common_sync/my-toolbox/src/my_toolbox/vim"
cp "$VIM_DIR/basic.vim" ~/.vimrc
cat "$VIM_DIR/remote.vim" >> ~/.vimrc

# setup neovim to use the same config as vim
mkdir -p ~/.config/nvim
echo -e "set runtimepath^=~/.vim runtimepath+=~/.vim/after\nlet &packpath = &runtimepath\nsource ~/.vimrc" > ~/.config/nvim/init.vim

echo "Setup completed!"
