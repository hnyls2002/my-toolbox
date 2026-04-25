#!/bin/bash
set -e

# apt utils
apt update
apt install -y neovim rsync

# pip utils
pip install nvitop gpustat

# rust toolchain (required by some sglang python build deps)
if ! command -v cargo >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
fi
export PATH="$HOME/.cargo/bin:$PATH"

# my-toolbox (sglang worktree is installed separately via install_worktree.sh)
ln -sfn /host_home/common_sync/my-toolbox /root/my-toolbox
cd /root/my-toolbox && pip install -e . --config-settings editable_mode=compat

# setup tmux
echo "set -g mouse on" >> /root/.tmux.conf
echo "setw -g mode-keys vi" >> /root/.tmux.conf
echo "set -g history-limit 100000" >> /root/.tmux.conf

# setup vimrc
VIM_DIR="/host_home/common_sync/my-toolbox/src/my_toolbox/vim"
cp "$VIM_DIR/basic.vim" ~/.vimrc
cat "$VIM_DIR/remote.vim" >> ~/.vimrc

# setup neovim to use the same config as vim
mkdir -p ~/.config/nvim
echo -e "set runtimepath^=~/.vim runtimepath+=~/.vim/after\nlet &packpath = &runtimepath\nsource ~/.vimrc" > ~/.config/nvim/init.vim

echo "Setup completed!"
