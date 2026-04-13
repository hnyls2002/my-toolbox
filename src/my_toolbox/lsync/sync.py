import shlex
import subprocess
from pathlib import Path
from typing import Optional

import typer

from my_toolbox.config import get_nda_dirs, rdev_defaults, rdev_servers
from my_toolbox.lsync.git_meta import GitMetaCollector
from my_toolbox.lsync.sync_log import Logger
from my_toolbox.lsync.sync_tree import SyncTree
from my_toolbox.ui import (
    CursorTool,
    UITool,
    bold,
    dim,
    format_hosts,
    green_text,
    red_text,
    section_header,
    strikethrough,
    warn_banner,
    yellow_text,
)

logger = Logger()
app = typer.Typer()

LSYNC_DIR = Path(__file__).parent
RSYNCIGNORE = LSYNC_DIR / ".lsyncignore"


def _sync_command(
    server: str,
    remote_dir: str,
    local_dir: str,
    tree: SyncTree,
    delete: bool = False,
    git_repo: bool = False,
    git_ignore: Optional[str] = None,
    quiet: bool = False,
):
    src_dir = Path(local_dir)
    dst_dir = Path(remote_dir)

    src_dirs = [src_dir / d for d in tree.sync_dirs if (src_dir / d).exists()]

    if server.endswith("-nda"):
        nda_dir_names = get_nda_dirs()
        nda_dirs = [src_dir / d for d in nda_dir_names if (src_dir / d).exists()]
        src_dirs += nda_dirs
        if not quiet:
            print(f"  {yellow_text('NDA')}: {', '.join(nda_dir_names)}")

    if tree.git_meta_dir.is_dir():
        src_dirs.append(tree.git_meta_dir)

    rsync_cmd = [
        "rsync",
        "-rlth",
        "--no-perms",
        "--chmod=ugo=rwX",
        "--delete" if delete else "",
        "" if quiet else "--info=progress2",
        f"--exclude-from={git_ignore}" if git_ignore else "",
        f"--exclude-from={RSYNCIGNORE}",
        "--exclude=.git" if not git_repo else "",
    ]

    src_dirs_str = [d.as_posix().rstrip("/") for d in src_dirs]
    dst_dir_str = dst_dir.as_posix().rstrip("/")
    rsync_cmd.extend(src_dirs_str)
    rsync_cmd.append(dst_dir_str)

    rsync_cmd = [cmd for cmd in rsync_cmd if cmd]
    if not quiet:
        typer.echo(f"\n  {dim('$ ' + ' '.join(rsync_cmd))}")

    return rsync_cmd


class SyncTool:
    def __init__(
        self,
        server: str,
        server_config: dict,
        file_or_path: Optional[str],
        delete: bool,
        git_repo: bool,
        yes: bool = False,
        quiet: bool = False,
    ):
        self.server = server
        self.server_config = server_config
        self.hosts = self.server_config["hosts"]
        self.tree = SyncTree()
        self.is_full_sync = file_or_path is None

        if self.is_full_sync:
            self.local_dir = self.tree.sync_root
            self.remote_dir = Path(self.server_config["base_dir"]) / self.local_dir.name
        else:
            self.local_dir = Path.cwd() / file_or_path
            relative_path = self.local_dir.relative_to(self.tree.sync_root.parent)
            self.remote_dir = Path(self.server_config["base_dir"]) / relative_path

        self.delete = delete
        self.git_repo = git_repo
        self.yes = yes
        self.quiet = quiet
        self.git_ignore = self._probe_gitignore()

        self.__post_init__()
        if not self.quiet:
            CursorTool.clear_screen()

            if self.delete:
                typer.echo(warn_banner("Delete mode enabled"))
                typer.echo("")

            logger.print_last_log()

            relative_path = self.local_dir.relative_to(self.tree.sync_root.parent)
            typer.echo(section_header("Sync Plan"))
            typer.echo(f"  Source:  {bold(str(relative_path))}")
            typer.echo(f"  Target:  {format_hosts(self.hosts)}")
            if self.delete:
                typer.echo(f"  Delete:  {yellow_text('Yes')}")
            if self.git_repo:
                typer.echo(f"  Git:     Yes")

    def __post_init__(self):
        if not isinstance(self.hosts, list):
            self.hosts = [self.hosts]

    def _probe_gitignore(self) -> Optional[str]:
        gitignore_file = self.local_dir / ".gitignore"
        return gitignore_file.as_posix() if gitignore_file.exists() else None

    def _ui_thread(self, rsync_procs: list[subprocess.Popen]):
        with UITool.ui_tool(len(rsync_procs), desc="Rsync") as ui_tool:
            while not all(p.poll() is not None for p in rsync_procs):
                for i, p in enumerate(rsync_procs):
                    if p.stdout and (char := p.stdout.read(1)):
                        rendered_char = char if char in {"\n", "\r"} else dim(char)
                        ui_tool.update_char(i, rendered_char)

    def _allowed_remote_dirs(self) -> set[str]:
        """Return the set of directory names that should exist on the remote."""
        src_dir = self.local_dir
        allowed = {d for d in self.tree.sync_dirs if (src_dir / d).exists()}
        if self.server.endswith("-nda"):
            allowed.update(d for d in get_nda_dirs() if (src_dir / d).exists())
        if self.tree.git_meta_dir.is_dir():
            allowed.add(self.tree.git_meta_dir.name)
        return allowed

    def _cleanup_remote_stale_dirs(self):
        """Remove remote directories not in the local sync scope."""
        allowed = self._allowed_remote_dirs()
        remote_root = self.remote_dir.as_posix()

        all_stale: list[tuple[str, set[str]]] = []
        for host in self.hosts:
            try:
                result = subprocess.run(
                    ["ssh", host, f"ls -1 {shlex.quote(remote_root)}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                continue
            if result.returncode != 0:
                continue
            remote_dirs = {d for d in result.stdout.splitlines() if d}
            stale = remote_dirs - allowed
            if stale:
                all_stale.append((host, stale))

        if not all_stale:
            return

        # Collect unique stale dir names across all hosts
        all_stale_names = sorted(set().union(*(s for _, s in all_stale)))

        typer.echo(f"\n  {yellow_text('⚠')} Stale remote directories:")
        for d in all_stale_names:
            hosts_with = [h for h, s in all_stale if d in s]
            typer.echo(
                f"    {strikethrough(d)}" f"  {dim('on')} {format_hosts(hosts_with)}"
            )

        typer.echo()
        typer.echo(f"  {dim('Remove? [y/N]')} ", nl=False)
        answer = input().strip().lower()
        if answer != "y":
            typer.echo(f"  {dim('Skipped.')}")
            return

        for host, stale in all_stale:
            rm_targets = " ".join(
                shlex.quote(f"{remote_root}/{d}") for d in sorted(stale)
            )
            cmd = f"rm -rf {rm_targets}"
            try:
                subprocess.run(
                    ["ssh", host, cmd],
                    capture_output=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                typer.echo(f"    {yellow_text('!')} {host}: timeout, skipped")

        typer.echo(f"  {green_text('✓')} Stale dirs removed")

    def _preflight_permission_check(self):
        """SSH and find first non-writable path under each remote sync dir."""
        remote_root = self.remote_dir.as_posix()

        failed: list[tuple[str, str]] = []
        for host in self.hosts:
            try:
                result = subprocess.run(
                    [
                        "ssh",
                        host,
                        f"find {shlex.quote(remote_root)} -not -writable -print -quit 2>/dev/null",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                continue
            path = result.stdout.strip()
            if path:
                failed.append((host, path))

        if not failed:
            return

        typer.echo(
            f"\n  {yellow_text('⚠')} Non-writable path detected (likely root-owned from Docker):"
        )
        for host, path in failed:
            typer.echo(f"    {bold(host)}: {dim(path)}")

        typer.echo(f"\n    Fixing permissions via docker exec...")
        base_dir = self.server_config["base_dir"]
        container_root = "/host_home" + remote_root.removeprefix(base_dir)
        fix_failed = []
        for host, _ in failed:
            cmd = (
                f"docker exec {rdev_defaults()['container']} "
                f"chmod -R 777 {shlex.quote(container_root)}"
            )
            typer.echo(f"    {dim(f'$ ssh {host} {cmd}')}")
            result = subprocess.run(
                ["ssh", host, cmd],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                typer.echo(
                    f"    {red_text('✗')} Failed on {host}: {result.stderr.strip()}"
                )
                fix_failed.append(host)

        if fix_failed:
            raise typer.Exit(1)

        typer.echo(f"    {green_text('✓')} Permissions fixed, continuing sync...\n")

    def sync(self):
        GitMetaCollector(self.tree).collect_all()

        rsync_cmds = []
        for host in self.hosts:
            # trailing slash tells rsync to sync directory contents
            is_folder = "/" if self.local_dir.is_dir() else ""
            rsync_cmds.append(
                _sync_command(
                    self.server,
                    f"{host}:{self.remote_dir.as_posix()}{is_folder}",
                    f"{self.local_dir.as_posix()}{is_folder}",
                    self.tree,
                    self.delete,
                    self.git_repo,
                    self._probe_gitignore(),
                    quiet=self.quiet,
                )
            )

        if not self.yes:
            input(dim("\n  ⏎  Press Enter to continue..."))
        if not self.quiet:
            CursorTool.clear_screen()

            relative_path = self.local_dir.relative_to(self.tree.sync_root.parent)
            typer.echo(
                section_header(f"Syncing {relative_path} @ {format_hosts(self.hosts)}")
            )

        self._preflight_permission_check()

        rsync_procs: list[subprocess.Popen] = []
        for cmd in rsync_cmds:
            rsync_procs.append(
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE if not self.quiet else subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            )

        if self.quiet:
            for p in rsync_procs:
                p.wait()
        else:
            self._ui_thread(rsync_procs)

        # Collect rsync errors, group by error type across hosts
        _KNOWN_ERRORS = ["No space left on device", "Permission denied"]
        error_hosts: dict[str, list[str]] = {}
        other_errors: list[tuple[str, str]] = []

        for rsync_proc, host in zip(rsync_procs, self.hosts):
            rsync_proc.wait()
            if rsync_proc.returncode == 0:
                continue
            stderr = rsync_proc.stderr.read() if rsync_proc.stderr else ""
            matched_patterns: set[str] = set()
            for line in stderr.splitlines():
                for pattern in _KNOWN_ERRORS:
                    if pattern.lower() in line.lower():
                        matched_patterns.add(pattern)
                        break
            for pattern in matched_patterns:
                error_hosts.setdefault(pattern, []).append(host)
            if not matched_patterns and stderr.strip():
                other_errors.append((host, stderr.strip()))

        if self.delete and self.is_full_sync:
            self._cleanup_remote_stale_dirs()

        if error_hosts or other_errors:
            typer.echo()
            for pattern, hosts in error_hosts.items():
                typer.echo(f"  {red_text('✗')} {pattern}: {format_hosts(hosts)}")
            for host, stderr in other_errors:
                typer.echo(f"  {red_text('✗')} rsync failed on {bold(host)}:")
                for line in stderr.splitlines()[:5]:
                    typer.echo(f"    {dim(line)}")
            raise typer.Exit(1)

        logger.log_one(
            path=self.local_dir.relative_to(self.tree.sync_root.parent),
            hosts=self.hosts,
            delete=self.delete,
            git_repo=self.git_repo,
        )

        if self.quiet:
            typer.echo(f"{green_text('✓')} Synced @ {format_hosts(self.hosts)}")
        else:
            last = logger.read_last_sync_log()
            if last:
                typer.echo(
                    f"{green_text('✓')} Done  "
                    f"{dim(last.now_str)}  {last.path} @ {format_hosts(last.hosts)}"
                )


@app.command()
def sync(
    server: str = typer.Option(..., "--server", "-n"),
    file_or_path: Optional[str] = typer.Option(None, "--file-or-path", "-f"),
    delete: bool = typer.Option(False, "--delete", "-d"),
    git_repo: bool = typer.Option(False, "--git", "-g", help="sync git repo"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip confirmation"),
):
    servers = rdev_servers()

    if server not in servers:
        raise typer.Exit(f"Invalid server(cluster) name: {server}")

    sync_tool = SyncTool(
        server,
        servers[server],
        file_or_path=file_or_path,
        delete=delete,
        git_repo=git_repo,
        yes=yes,
    )
    sync_tool.sync()


if __name__ == "__main__":
    app()
