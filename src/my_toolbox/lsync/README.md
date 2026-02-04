# Customized File Synchronization Utility

Usage

```bash
export LSYNC_DIR="/path/to/lsync"
alias lsync="python $SCRIPT_DIR/lsync.py"
```

TODO:

- [ ] move .lsyncignore to top folders
- [ ] support viewing logs from remote servers (for completely ignoring .git folders)
- [ ] support viewing size to sync (--dry-run)
- [ ] support viewing total size excluding ignored files
