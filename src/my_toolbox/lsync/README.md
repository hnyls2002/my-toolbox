# lsync — File Synchronization Utility

## Install

```bash
pip install git+https://github.com/hnyls2002/my-toolbox.git
```

## Usage

```bash
lsync --server <server-name> [-f <path>] [-d] [-g]
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SYNC_ROOT` | Yes | Path to the sync workspace root |
| `LSYNC_NDA_DIRS` | No | Comma-separated NDA directories (synced when server name ends with `-nda`) |
| `LSYNC_EXTRA_SYNC_DIRS` | No | Comma-separated extra directories to sync beyond the base set + worktrees |

## Config

Server definitions live in `~/.lsync.yaml`:

```yaml
my-server:
  hosts: user@host        # or a list of hosts
  base_dir: /remote/path
```

## TODO

- [ ] move .lsyncignore to top folders
- [ ] support viewing logs from remote servers (for completely ignoring .git folders)
- [ ] support viewing size to sync (--dry-run)
- [ ] support viewing total size excluding ignored files
