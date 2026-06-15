#!/bin/bash
# Pre-sync bootstrap for rx devboxes. Piped over ssh stdin by `rdev devbox-init`
# BEFORE any code is synced (rsync itself is installed here), so it must be
# self-contained -- nothing under /mirror/common_sync is synced yet.
set -e
export DEBIAN_FRONTEND=noninteractive

# rsync: prerequisite for rdev sync; zsh: interactive shell (parity with the
# raw container's `docker exec -it ... zsh`)
apt-get update -q
apt-get install -y -q rsync zsh

# Persist /root/.cache (pip/HF/...) on /personal, which survives across all
# devboxes on the cluster -- parity with the raw flow's host /data/.cache mount.
if [ -d /personal ]; then
    mkdir -p /personal/.cache
    if [ -d /root/.cache ] && [ ! -L /root/.cache ]; then
        cp -a /root/.cache/. /personal/.cache/ 2>/dev/null || true
        rm -rf /root/.cache
    fi
    ln -sfn /personal/.cache /root/.cache
else
    echo "warn: /personal not mounted; /root/.cache stays pod-local (wiped on release)" >&2
fi

# Land in zsh on `ssh <devbox>` / `rdev shell <devbox>`
chsh -s "$(command -v zsh)" root

echo "devbox bootstrap completed"
