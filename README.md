# my-toolbox

Personal tiny tools, all in one place.

## Install

```bash
pip install --force-reinstall git+https://github.com/hnyls2002/my-toolbox.git
```

## Tools

### `rgit` — Unified git toolkit

| Subcommand | Description |
|------------|-------------|
| `rgit log / status / branch / diff / diff-stat` | Common git info viewers |
| `rgit collect` | Refresh git metadata for repos under sync_root |
| `rgit prune` | Interactively select and delete local + remote branches |
| `rgit repo list` | List all repos with cached metadata |
| `rgit repo status` | Compact status summary for all repos |
| `rgit worktree list` | List available worktrees |
| `rgit worktree install` | Switch installed worktree (`pip install -e`) |
| `rgit id show / list` | Show current identity or list profiles |
| `rgit id use / add / remove` | Switch, add, or remove git identity profiles |

### `rgh` — GitHub CLI helper

| Subcommand | Description |
|------------|-------------|
| `rgh cancel <url>` | Cancel a GitHub Actions workflow run |
| `rgh checkout <pr>` | Checkout a PR into a new git worktree |

### `lsync` — Multi-host sync

| Subcommand | Description |
|------------|-------------|
| `lsync sync` | Sync local directories to remote servers via rsync |

### `rgrip` — Local markdown preview

| Subcommand | Description |
|------------|-------------|
| `rgrip open <file>` | Start background preview session (python-markdown + pygments) |
| `rgrip list` | List active sessions |
| `rgrip stop <file\|port>` | Stop a session (`--all` to stop all) |
| `rgrip browse <file\|port>` | Open session in browser |

### `sgl-run` — SGLang model launcher

Single entry point with args for model config, speculative decoding, TP/DP parallelism, and disaggregated serving.

### Docker tools

| Command | Description |
|---------|-------------|
| `run-docker` | Container orchestration with auto-setup and mount config |
| `docker-exec` | SSH into remote host and exec inside a container |
| `reconfig-docker` | Reconfigure Docker daemon (data-root, NVIDIA runtime) |

### Utilities

| Command | Description |
|---------|-------------|
| `whousesgpu` | Monitor GPU process usage with optional watch mode |
| `bulk-sync` | Bulk rsync transfer between machines |
| `list-ib-devices` | List InfiniBand devices and link states |
| `fmt-sh` | Format compact shell commands into readable multi-line format |
