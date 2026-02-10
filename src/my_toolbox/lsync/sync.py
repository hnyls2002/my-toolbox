import os
import subprocess
from pathlib import Path
from typing import Optional

import typer
import yaml

from my_toolbox.lsync.git_meta import GitMetaCollector
from my_toolbox.lsync.sync_log import Logger
from my_toolbox.lsync.sync_tree import SyncTree
from my_toolbox.lsync.ui import (
    CursorTool,
    UITool,
    bold,
    dim,
    green_text,
    section_header,
    warn_banner,
    yellow_text,
)
from my_toolbox.lsync.utils import popen_with_error_check

logger = Logger()
app = typer.Typer()

LSYNC_DIR = Path(__file__).parent
DEFAULT_CONFIG = Path.home() / ".lsync.yaml"
RSYNCIGNORE = LSYNC_DIR / ".lsyncignore"
NDA_DIRS = (
    os.environ.get("LSYNC_NDA_DIRS", "").split(",")
    if os.environ.get("LSYNC_NDA_DIRS")
    else []
)


def _sync_command(
    server: str,
    remote_dir: str,
    local_dir: str,
    tree: SyncTree,
    delete: bool = False,
    git_repo: bool = False,
    git_ignore: Optional[str] = None,
):
    src_dir = Path(local_dir)
    dst_dir = Path(remote_dir)

    src_dirs = [src_dir / d for d in tree.sync_dirs if (src_dir / d).exists()]

    if server.endswith("-nda"):
        nda_dirs = [src_dir / d for d in NDA_DIRS if (src_dir / d).exists()]
        src_dirs += nda_dirs
        print(f"  {yellow_text('NDA')}: {', '.join(NDA_DIRS)}")

    if tree.git_meta_dir.is_dir():
        src_dirs.append(tree.git_meta_dir)

    rsync_cmd = [
        "rsync",
        "-ah",
        "--delete" if delete else "",
        "--info=progress2",
        f"--exclude-from={git_ignore}" if git_ignore else "",
        f"--exclude-from={RSYNCIGNORE}",
        "--exclude=.git" if not git_repo else "",
    ]

    src_dirs_str = [d.as_posix().rstrip("/") for d in src_dirs]
    dst_dir_str = dst_dir.as_posix().rstrip("/")
    rsync_cmd.extend(src_dirs_str)
    rsync_cmd.append(dst_dir_str)

    rsync_cmd = [cmd for cmd in rsync_cmd if cmd]
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
    ):
        self.server = server
        self.server_config = server_config
        self.hosts = self.server_config["hosts"]
        self.tree = SyncTree()

        if file_or_path is None:
            self.local_dir = self.tree.sync_root
            self.remote_dir = Path(self.server_config["base_dir"]) / self.local_dir.name
        else:
            self.local_dir = Path.cwd() / file_or_path
            relative_path = self.local_dir.relative_to(self.tree.sync_root.parent)
            self.remote_dir = Path(self.server_config["base_dir"]) / relative_path

        self.delete = delete
        self.git_repo = git_repo
        self.git_ignore = self._probe_gitignore()

        self.__post_init__()
        CursorTool.clear_screen()

        if self.delete:
            typer.echo(warn_banner("Delete mode enabled"))
            typer.echo("")

        logger.print_last_log()

        relative_path = self.local_dir.relative_to(self.tree.sync_root.parent)
        typer.echo(section_header("Sync Plan"))
        typer.echo(f"  Source:  {bold(str(relative_path))}")
        typer.echo(f"  Target:  {bold(str(self.hosts))}")
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
                        ui_tool.update_char(i, char)

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
                )
            )

        input(dim("\n  ⏎  Press Enter to continue..."))
        CursorTool.clear_screen()

        relative_path = self.local_dir.relative_to(self.tree.sync_root.parent)
        typer.echo(section_header(f"Syncing {relative_path} -> {self.hosts}"))

        rsync_procs: list[subprocess.Popen] = []
        for cmd in rsync_cmds:
            rsync_procs.append(popen_with_error_check(cmd))

        self._ui_thread(rsync_procs)

        for rsync_proc in rsync_procs:
            rsync_proc.wait()

        logger.log_one(
            path=self.local_dir.relative_to(self.tree.sync_root.parent),
            hosts=self.hosts,
            delete=self.delete,
            git_repo=self.git_repo,
        )

        last = logger.read_last_sync_log()
        if last:
            typer.echo(
                f"{green_text('✓')} Done  "
                f"{dim(last.now_str)}  {last.path} -> {last.hosts}"
            )


@app.command()
def sync(
    server: str = typer.Option(..., "--server", "-n"),
    file_or_path: Optional[str] = typer.Option(None, "--file-or-path", "-f"),
    delete: bool = typer.Option(False, "--delete", "-d"),
    git_repo: bool = typer.Option(False, "--git", "-g", help="sync git repo"),
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
):
    with open(config, "r") as f:
        config_dict = yaml.safe_load(f)

    if server not in config_dict:
        raise typer.Exit(f"Invalid server(cluster) name: {server}")

    sync_tool = SyncTool(
        server,
        config_dict[server],
        file_or_path=file_or_path,
        delete=delete,
        git_repo=git_repo,
    )
    sync_tool.sync()


if __name__ == "__main__":
    app()
