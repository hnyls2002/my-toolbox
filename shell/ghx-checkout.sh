#!/usr/bin/env bash
# Source this file in ~/.zshrc or ~/.bashrc:
#   source /path/to/my-toolbox/shell/ghx-checkout.sh

ghx-checkout() {
    local dir
    dir=$(ghx checkout "$@") && cd "$dir"
}
