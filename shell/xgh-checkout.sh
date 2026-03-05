#!/usr/bin/env bash
# Source this file in ~/.zshrc or ~/.bashrc:
#   source /path/to/my-toolbox/shell/xgh-checkout.sh

xgh-checkout() {
    local dir
    dir=$(xgh checkout "$@") && cd "$dir"
}
