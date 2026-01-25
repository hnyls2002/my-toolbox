import os
import subprocess
from pathlib import Path
from typing import Optional

import typer
import yaml

from sync_log import Logger
from ui import CursorTool, UITool, blue_block, red_block, yellow_block
from utils import get_lsync_dir, popen_with_error_check

logger = Logger()

app = typer.Typer()

LSYNC_DIR = get_lsync_dir()
WHITE_LISTED_DIRS = ["scripts", "sglang"]

# TODO: move this into config file
TOP_DIRS = ["common_sync"]
DEFAULT_CONFIG = f"{LSYNC_DIR}/lsync_config.yaml"
RSYNCIGNORE = f"{LSYNC_DIR}/.lsyncignore"
NDA_DIRS = (
    os.environ.get("LSYNC_NDA_DIRS", "").split(",")
    if os.environ.get("LSYNC_NDA_DIRS")
    else []
)


def _sync_command(
    server: str,
    remote_dir: str,
    local_dir: str,
    delete: bool = False,
    git_repo: bool = False,
    git_ignore: Optional[str] = None,
):
    src_dir, dst_dir = Path(local_dir), Path(remote_dir)

    src_dirs = [src_dir / d for d in WHITE_LISTED_DIRS if (src_dir / d).exists()]

    # Only include NDA directories for NDA servers
    if server.endswith("-nda"):
        nda_dirs = [src_dir / d for d in NDA_DIRS if (src_dir / d).exists()]
        src_dirs += nda_dirs
        print(red_block(f'Including NDA directories "{", ".join(NDA_DIRS)}"'))

    rsync_cmd = [
        "rsync",
        "-ah",
        "--delete" if delete else "",
        "--info=progress2",
        f"--exclude-from={git_ignore}" if git_ignore else "",
        f"--exclude-from={RSYNCIGNORE}",
        "--exclude=.git" if not git_repo else "",
    ]

    src_dirs_str = [f"{d.as_posix().rstrip('/')}" for d in src_dirs]
    dst_dir_str = f"{dst_dir.as_posix().rstrip('/')}"
    rsync_cmd.extend(src_dirs_str)
    rsync_cmd.append(dst_dir_str)

    # remove empty strings
    rsync_cmd = [cmd for cmd in rsync_cmd if cmd]
    typer.echo(f"Executing: \x1b[42m{' '.join(rsync_cmd)}\x1b[0m")

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
        self.ancestor_to_sync = self.find_ancestor_to_sync()

        if file_or_path is None:
            self.local_dir = self.ancestor_to_sync
            self.remote_dir = Path(self.server_config["base_dir"]) / self.local_dir.name
        else:
            self.local_dir = Path.cwd() / file_or_path
            relative_path = self.local_dir.relative_to(self.ancestor_to_sync.parent)
            self.remote_dir = Path(self.server_config["base_dir"]) / relative_path

        # arguments
        self.delete = delete
        self.git_repo = git_repo
        self.git_ignore = self._probe_gitignore()

        self.__post_init__()

        CursorTool.clear_screen()

        # Info
        if self.delete:
            typer.echo(
                f"{yellow_block('#'*28)}\n"
                f"{yellow_block('# Delete option is enabled #')}\n"
                f"{yellow_block('#'*28)}"
            )

        logger.print_last_log()

        src, dst = ("macbook", self.hosts)
        relative_path = self.local_dir.relative_to(self.ancestor_to_sync.parent)
        typer.echo(
            f"Syncing folder {blue_block(relative_path)} from "
            f"{blue_block(src)} -> {blue_block(dst)} "
        )

    def __post_init__(self):
        if not isinstance(self.hosts, list):
            self.hosts = [self.hosts]

    def find_ancestor_to_sync(self) -> Path:
        d = Path.cwd()
        while d.as_posix() != "/":
            if d.name in TOP_DIRS:
                return d
            d = d.parent
        raise typer.Exit(f"No ancestor directory in {TOP_DIRS} found in {Path.cwd()}")

    def _probe_gitignore(self) -> Optional[str]:
        gitignore_file = self.local_dir / ".gitignore"
        return gitignore_file.as_posix() if gitignore_file.exists() else None

    def _ui_thread(self, rsync_procs: list[subprocess.Popen]):
        with UITool.ui_tool(len(rsync_procs)) as ui_tool:
            while not all(p.poll() is not None for p in rsync_procs):
                for i, p in enumerate(rsync_procs):
                    if p.stdout and (char := p.stdout.read(1)):
                        ui_tool.update_char(i, char)

    def sync(self):
        rsync_cmds = []
        for host in self.hosts:
            # adding trailing slash to sync the content of the directory
            is_folder = "/" if self.local_dir.is_dir() else ""
            rsync_cmds.append(
                _sync_command(
                    self.server,
                    f"{host}:{self.remote_dir.as_posix()}{is_folder}",
                    f"{self.local_dir.as_posix()}{is_folder}",
                    self.delete,
                    self.git_repo,
                    self._probe_gitignore(),
                )
            )

        input("Press Enter to continue...")
        CursorTool.clear_screen()
        relative_path = self.local_dir.relative_to(self.ancestor_to_sync.parent)
        typer.echo(
            f"Syncing local folder {blue_block(relative_path)} with remote hosts {blue_block(self.hosts)}"
            f"\n(delete={self.delete})"
            f"\n(git_repo={self.git_repo})"
            f"\n===================================================================="
        )

        rsync_procs: list[subprocess.Popen] = []
        for cmd in rsync_cmds:
            rsync_procs.append(popen_with_error_check(cmd))

        self._ui_thread(rsync_procs)

        for rsync_proc in rsync_procs:
            rsync_proc.wait()

        logger.log_one(
            path=self.local_dir.relative_to(self.ancestor_to_sync.parent),
            hosts=self.hosts,
            delete=self.delete,
            git_repo=self.git_repo,
        )

        logger.print_last_log()


@app.command()
def sync(
    server: str = typer.Option(..., "--server", "-n"),
    file_or_path: Optional[str] = typer.Option(None, "--file-or-path", "-f"),
    delete: bool = typer.Option(False, "--delete", "-d"),
    git_repo: bool = typer.Option(False, "--git", "-g", help="sync git repo"),
    config: str = typer.Option(DEFAULT_CONFIG, "--config"),
):
    # read yaml from config
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
