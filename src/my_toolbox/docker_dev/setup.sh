#!/bin/bash
set -e

# install apt packages
apt update
apt install -y neovim

# pip install some utilities
pip install nvitop gpustat

ln -sf /host_home/common_sync/sglang /root/sglang
ln -sf /host_home/common_sync/my-toolbox /root/my-toolbox

# setup tmux
echo "set -g mouse on" >> /root/.tmux.conf
echo "setw -g mode-keys vi" >> /root/.tmux.conf
echo "set -g history-limit 100000" >> /root/.tmux.conf
tmux source-file /root/.tmux.conf

# TODO: setup nvim

cd /root/sglang/python
pip install -e . --config-settings editable_mode=compat

echo "Setup completed!"
