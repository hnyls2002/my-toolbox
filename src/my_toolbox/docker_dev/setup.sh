#!/bin/bash
set -e

# apt utils
apt update
apt install -y neovim

# pip utils
pip install nvitop gpustat

# link common sync dirs
ln -sf /host_home/common_sync/sglang /root/sglang
ln -sf /host_home/common_sync/my-toolbox /root/my-toolbox

cd /root/sglang/python && pip install -e . --config-settings editable_mode=compat
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
