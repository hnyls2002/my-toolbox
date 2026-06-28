# my-toolbox

Personal tiny tools, all in one place.

## Install

```bash
pip install --force-reinstall git+https://github.com/hnyls2002/my-toolbox.git
```

## Configuration

All remote-dev configs (servers, docker, sync) are unified in `~/.rdev/config.yaml`.
Managed via [rdev-dotfiles](https://github.com/hnyls2002/rdev-dotfiles) (private).

```bash
# Install dotfiles (creates ~/.rdev/ symlinks)
cd ~/common_sync/rdev-dotfiles && ./install.sh
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SYNC_ROOT` | Yes | Absolute path to the local sync workspace root (e.g. `~/common_sync`) |
| `RDEV_NDA_DIRS` | No | Comma-separated list of NDA directories. Only synced when the server name ends with `-nda` |

Set in your shell profile (e.g. `~/.zshrc`):

```bash
export SYNC_ROOT=$HOME/common_sync
export RDEV_NDA_DIRS=MyProject,OtherProject
```

## Shell Completion

All typer-based CLIs (`rdev`, `rgit`, `rgh`, `rgrip`) support zsh completion.
One-time setup — run once per tool:

```bash
SHELL=/bin/zsh <tool> --install-completion
```

This puts a completion script in `~/.zfunc/` and adds fpath to `.zshrc`. Restart the terminal to take effect.

## Tools

### `rdev` — Remote development CLI

| Subcommand | Description |
|------------|-------------|
| `rdev sync <target>` | Sync code to server group or single host |
| `rdev shell <host>` | Ensure container + interactive shell |
| `rdev exec <host> <cmd>` | Sync + ensure container + execute command |
| `rdev install <host>` | Sync + reinstall one worktree (editable) without recreating the container |
| `rdev status [target]` | Show container state (+ `--gpu` for per-GPU usage) |
| `rdev ctr create <target>` | Create container (runs setup only on new containers) |
| `rdev ctr start / stop / restart <target>` | Container lifecycle actions |
| `rdev ctr recreate <target>` | Remove + pull + create fresh (e.g. image drift) |

> **Sync scope (default is no longer full):** `rdev sync` and `rdev exec` sync **only the checkout folder your cwd is in** under `common_sync/`. Use `--all` for a full sync of every tracked dir, or `--only a,b` to pick specific ones. Running from the `common_sync/` top level falls back to a full sync; running outside `SYNC_ROOT` errors. (`rdev ctr create` still does a full sync — fresh containers want everything.)

> **Live output rendering:** `rdev exec` and the pip/pull/setup steps of `ctr create`/`recreate`/`install`/`devbox-init` render their output in a dim fixed-height scrolling window (docker build / cargo style) when stdout is a terminal — the last N lines stay visible while older lines scroll off the top. Falls back to plain pass-through when piped/redirected (CI, `> file`). `rdev exec` now also propagates the remote command's exit code.

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

### `rgrip` — Local markdown preview

| Subcommand | Description |
|------------|-------------|
| `rgrip open <file>` | Start background preview session (python-markdown + pygments) |
| `rgrip list` | List active sessions |
| `rgrip stop <file\|port>` | Stop a session (`--all` to stop all) |
| `rgrip browse <file\|port>` | Open session in browser |

### `sgl-run` — SGLang model launcher

Single entry point with args for model config, speculative decoding, TP/DP parallelism, and disaggregated serving.

### `gpt-kit` — ChatGPT history TUI

Browse and **batch-delete** your own ChatGPT conversations from a Textual TUI.

```bash
gpt-kit          # launch the TUI
gpt-kit check    # verify Chrome automation + ChatGPT login are ready
```

**How it works (nothing to paste).** ChatGPT's `backend-api` is behind Cloudflare,
which blocks plain HTTP clients. So gpt-kit drives your already-logged-in Chrome:
it runs same-origin requests inside a `chatgpt.com` tab via AppleScript — exactly
how the web app's own sidebar loads. The browser clears Cloudflare and the access
token is read automatically from the page's session, so there is nothing to paste.

macOS + Google Chrome only. One-time setup:

- Chrome menu -> **View -> Developer -> Allow JavaScript from Apple Events**
- On first run, allow the "Terminal wants to control Google Chrome" prompt
- Be logged into ChatGPT in Chrome (it reuses any open `chatgpt.com` tab, else
  opens one)

Run `gpt-kit check` to confirm both are green before launching the TUI.

| Key | Action |
|-----|--------|
| `j` / `k` | move down / up (vim) |
| `gg` / `G` | jump to top / bottom |
| `space` | toggle selection (survives filtering) |
| `V` … `j`/`k` | visual mode: extend a range (`V` commits, `Esc` cancels) |
| `/` / type | filter conversations by title |
| `a` / `n` | select all shown / clear selection |
| `d` | delete selected (asks to confirm) |
| `r` / `q` | refresh / quit |

> Delete sends `PATCH is_visible=false` (same as the web UI's delete) through the
> browser. Uses ChatGPT's unofficial `backend-api`; it can change without notice.

### Docker tools

| Command | Description |
|---------|-------------|
| `reconfig-docker` | Reconfigure Docker daemon (data-root, NVIDIA runtime) |

### Utilities

| Command | Description |
|---------|-------------|
| `bulk-sync` | Bulk rsync transfer between machines |
| `list-ib-devices` | List InfiniBand devices and link states |
| `fmt-sh` | Format compact shell commands into readable multi-line format |
