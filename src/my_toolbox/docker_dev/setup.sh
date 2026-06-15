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
