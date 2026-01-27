#!/bin/bash
set -e

# install apt packages
apt update
apt install -y neovim

# pip install some utilities
pip install nvitop

ln -sf /host_home/common_sync/sglang /root/sglang
ln -sf /host_home/common_sync/my-toolbox /root/my-toolbox

cd /root/sglang/python
pip install -e . --config-settings editable_mode=compat

echo "Setup completed!"
