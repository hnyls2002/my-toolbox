#!/bin/bash
set -e

ln -sf /host_home/common_sync/sglang /root/sglang

cd /root/sglang/python
pip install -e . --config-settings editable_mode=compat

echo "Setup completed!"
