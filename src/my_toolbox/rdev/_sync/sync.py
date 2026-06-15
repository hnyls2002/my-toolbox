import shlex
import subprocess
from pathlib import Path
from typing import Optional

import typer

from my_toolbox.rdev._sync.git_meta import GitMetaCollector
from my_toolbox.rdev._sync.sync_log import Logger
from my_toolbox.rdev._sync.sync_tree import SyncTree
from my_toolbox.rdev.container import check_container
from my_toolbox.rdev.topology import Instance
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

SYNC_DIR = Path(__file__).parent
RSYNCIGNORE = SYNC_DIR / ".rsyncignore"


def _ssh_argv(host: str, *, tty: bool = False) -> list[str]:
    """Build ssh argv. User/port/proxy come from ~/.ssh/config."""
    cmd = ["ssh"]
    if tty:
        cmd.append("-t")
    cmd.append(host)
    return cmd


def _rsync_target(host: str, remote_dir: str) -> str:
    """rsync target string: <host>:<remote_dir>. ssh config supplies user."""
    return f"{host}:{remote_dir}"


def _sync_command(
    remote_dir: str,
    local_dir: str,
    tree: SyncTree,
    delete: bool = False,
    git_repo: bool = False,
    git_ignore: Optional[str] = None,
    quiet: bool = False,
    only_dirs: Optional[list[str]] = None,
    dry_run: bool = False,
):
    src_dir = Path(local_dir)
    dst_dir = Path(remote_dir)

    use_relative = False  # use rsync -R to preserve nested paths like commit_msg/<d>/

    if only_dirs:
        # User explicitly named the dirs to sync; skip auto-included worktrees.
        src_dirs = [src_dir / d for d in only_dirs if (src_dir / d).exists()]
        missing = [d for d in only_dirs if not (src_dir / d).exists()]
        if missing and not quiet:
            print(f"  {yellow_text('skip missing')}: {', '.join(missing)}")

        # Also include commit_msg/<d> for each named dir (so remote rgit etc.
        # has fresh metadata for the synced repos). Use -R so the
        # `commit_msg/<d>` structure is preserved at the destination.
        if tree.git_meta_dir.is_dir():
            for d in only_dirs:
                sub = tree.git_meta_dir / d
                if sub.is_dir():
                    src_dirs.append(sub)
                    use_relative = True
    else:
        src_dirs = [src_dir / d for d in tree.sync_dirs if (src_dir / d).exists()]
        if tree.git_meta_dir.is_dir():
            src_dirs.append(tree.git_meta_dir)

    # In dry-run, --info=progress2 is meaningless (nothing is transferred);
    # swap to -v so rsync lists the would-be transfers / *deleting lines.
    progress_arg = "" if quiet else ("-v" if dry_run else "--info=progress2")
    rsync_cmd = [
        "rsync",
        "-rlth",
        "--no-perms",
        "--chmod=ugo=rwX",
        "-R" if use_relative else "",
        "--delete" if delete else "",
        "--dry-run" if dry_run else "",
        progress_arg,
        f"--exclude-from={git_ignore}" if git_ignore else "",
        f"--exclude-from={RSYNCIGNORE}",
        "--exclude=.git" if not git_repo else "",
    ]

    if use_relative:
        # rsync -R preserves the path *after* the `/./` marker — anchor it at src_dir.
        src_dirs_str = [
            f"{src_dir.as_posix()}/./{d.relative_to(src_dir).as_posix()}".rstrip("/")
            for d in src_dirs
        ]
    else:
        src_dirs_str = [d.as_posix().rstrip("/") for d in src_dirs]
    dst_dir_str = dst_dir.as_posix().rstrip("/")
    rsync_cmd.extend(src_dirs_str)
    rsync_cmd.append(dst_dir_str)

    rsync_cmd = [cmd for cmd in rsync_cmd if cmd]
    if not quiet:
        typer.echo(f"\n  {dim('$ ' + ' '.join(rsync_cmd))}")

    return rsync_cmd


class SyncTool:
    """Sync code to a set of instances. Each instance contributes its own
    remote target path and chmod-via-docker-exec container — even within
    one cluster — since instance-level overrides may differ."""

    def __init__(
        self,
        instances: list[Instance],
        file_or_path: Optional[str],
        delete: bool,
        git_repo: bool,
        yes: bool = False,
        quiet: bool = False,
        only_dirs: Optional[list[str]] = None,
        dry_run: bool = False,
    ):
        self.instances = instances
        self.tree = SyncTree()
        self.is_full_sync = file_or_path is None
        self.only_dirs = only_dirs

        if self.is_full_sync:
            self.local_dir = self.tree.sync_root
            self._remote_subpath = Path(self.local_dir.name)
        else:
            self.local_dir = Path.cwd() / file_or_path
            self._remote_subpath = self.local_dir.relative_to(
                self.tree.sync_root.parent
            )

        self.delete = delete
        self.git_repo = git_repo
        self.yes = yes
        self.quiet = quiet
        self.dry_run = dry_run
        self.git_ignore = self._probe_gitignore()

        if not self.quiet:
            CursorTool.clear_screen()

            if self.dry_run:
                typer.echo(warn_banner("Dry-run mode (no changes will be made)"))
                typer.echo("")
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
            if self.dry_run:
                typer.echo(f"  Dry-run: {yellow_text('Yes')}")
            if self.git_repo:
                typer.echo(f"  Git:     Yes")

    @property
    def hosts(self) -> list[str]:
        return [i.ssh.alias for i in self.instances]

    def _remote_dir_for(self, instance: Instance) -> Path:
        return instance.sync_target_base / self._remote_subpath

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
        if self.tree.git_meta_dir.is_dir():
            allowed.add(self.tree.git_meta_dir.name)
        return allowed

    def _cleanup_remote_stale_dirs(self):
        """Remove remote directories not in the local sync scope."""
        if self.only_dirs:
            # Partial sync: caller opted into a subset, so we have no basis for
            # deciding what's "stale" on the remote — leave everything else alone.
            return
        allowed = self._allowed_remote_dirs()

        all_stale: list[tuple[Instance, str, set[str]]] = []
        for inst in self.instances:
            remote_root = self._remote_dir_for(inst).as_posix()
            try:
                result = subprocess.run(
                    _ssh_argv(inst.ssh.alias) + [f"ls -1 {shlex.quote(remote_root)}"],
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
                all_stale.append((inst, remote_root, stale))

        if not all_stale:
            return

        # Collect unique stale dir names across all hosts
        all_stale_names = sorted(set().union(*(s for _, _, s in all_stale)))

        typer.echo(f"\n  {yellow_text('⚠')} Stale remote directories:")
        for d in all_stale_names:
            hosts_with = [inst.ssh.alias for inst, _, s in all_stale if d in s]
            typer.echo(
                f"    {strikethrough(d)}" f"  {dim('on')} {format_hosts(hosts_with)}"
            )

        if self.dry_run:
            typer.echo(f"\n  {dim('(dry-run: not removed)')}")
            return

        typer.echo()
        typer.echo(f"  {dim('Remove? [y/N]')} ", nl=False)
        answer = input().strip().lower()
        if answer != "y":
            typer.echo(f"  {dim('Skipped.')}")
            return

        for inst, remote_root, stale in all_stale:
            rm_targets = " ".join(
                shlex.quote(f"{remote_root}/{d}") for d in sorted(stale)
            )
            cmd = f"rm -rf {rm_targets}"
            try:
                subprocess.run(
                    _ssh_argv(inst.ssh.alias) + [cmd],
                    capture_output=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                typer.echo(f"    {yellow_text('!')} {inst.ssh.alias}: timeout, skipped")

        typer.echo(f"  {green_text('✓')} Stale dirs removed")

    def _preflight_permission_check(self):
        """SSH and find first non-writable path under each remote sync dir.

        Per-instance: each instance has its own container.name (instance-level
        override). chmod -R 777 is run inside that instance's container.

        Skipped for instances whose container isn't running — docker exec
        needs a running container; without one there's nothing useful we can
        do here, and rsync will surface any real permission issue itself.

        Symlinks are excluded from the writability check (`! -type l`): a
        broken symlink resolves to nothing and trips `-not -writable` even
        though it's not a docker-root-owned file at all.
        """
        failed: list[tuple[Instance, str, str]] = []  # (instance, remote_root, path)
        for inst in self.instances:
            # Devbox: ssh lands inside the container, files are owned by the
            # ssh user directly -- no docker-root ownership issue to fix.
            if inst.mode == "devbox":
                continue
            if check_container(inst.ssh.alias, inst.container.name) != "running":
                continue
            remote_root = self._remote_dir_for(inst).as_posix()
            try:
                result = subprocess.run(
                    _ssh_argv(inst.ssh.alias)
                    + [
                        f"find {shlex.quote(remote_root)} ! -type l -not -writable -print -quit 2>/dev/null"
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                continue
            path = result.stdout.strip()
            if path:
                failed.append((inst, remote_root, path))

        if not failed:
            return

        typer.echo(
            f"\n  {yellow_text('⚠')} Non-writable path detected (likely root-owned from Docker):"
        )
        for inst, _, path in failed:
            typer.echo(f"    {bold(inst.ssh.alias)}: {dim(path)}")

        typer.echo(f"\n    Fixing permissions via docker exec...")
        fix_failed = []
        for inst, remote_root, _ in failed:
            base_dir = inst.sync_target_base.as_posix()
            container_root = "/mirror" + remote_root.removeprefix(base_dir)
            cmd = (
                f"docker exec {inst.container.name} "
                f"chmod -R 777 {shlex.quote(container_root)}"
            )
            typer.echo(f"    {dim(f'$ ssh {inst.ssh.alias} {cmd}')}")
            result = subprocess.run(
                _ssh_argv(inst.ssh.alias) + [cmd],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                typer.echo(
                    f"    {red_text('✗')} Failed on {inst.ssh.alias}: {result.stderr.strip()}"
                )
                fix_failed.append(inst.ssh.alias)

        if fix_failed:
            raise typer.Exit(1)

        typer.echo(f"    {green_text('✓')} Permissions fixed, continuing sync...\n")

    def sync(self):
        # With --only, restrict git-meta collection to the requested dirs so
        # commit_msg/<d>/ stays fresh for the synced repos and we skip the
        # noise for everything else.
        GitMetaCollector(self.tree).collect_all(repo_names=self.only_dirs)

        rsync_cmds = []
        for inst in self.instances:
            # trailing slash tells rsync to sync directory contents
            is_folder = "/" if self.local_dir.is_dir() else ""
            remote_dir = self._remote_dir_for(inst).as_posix()
            target = _rsync_target(inst.ssh.alias, f"{remote_dir}{is_folder}")
            rsync_cmds.append(
                _sync_command(
                    target,
                    f"{self.local_dir.as_posix()}{is_folder}",
                    self.tree,
                    self.delete,
                    self.git_repo,
                    self._probe_gitignore(),
                    quiet=self.quiet,
                    only_dirs=self.only_dirs,
                    dry_run=self.dry_run,
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
