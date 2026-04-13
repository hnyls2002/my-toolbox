"""rdev CLI: unified remote development tool."""

from typing import Optional

import typer

from my_toolbox.config import rdev_server, rdev_servers
from my_toolbox.rdev.container import ensure_container, exec_in_container, run_setup

app = typer.Typer(help="Remote development CLI")


def _resolve_cfg(server: str, container: Optional[str] = None) -> dict:
    """Load server config, apply container override if given."""
    cfg = rdev_server(server)
    if container:
        cfg["container"] = container
    return cfg


def _sync(server: str) -> None:
    """Sync code to remote via lsync."""
    from my_toolbox.lsync.sync import SyncTool

    servers = rdev_servers()
    if server not in servers:
        raise typer.Exit(f"Unknown server: {server}")

    sync_tool = SyncTool(
        server,
        servers[server],
        file_or_path=None,
        delete=False,
        git_repo=False,
        yes=True,
    )
    sync_tool.sync()


@app.command()
def shell(
    server: str = typer.Argument(..., help="Server name from ~/.rdev/config.yaml"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    node_rank: int = typer.Option(0, "--node-rank", "-r", help="Node index"),
):
    """Sync + ensure container + interactive shell."""
    cfg = _resolve_cfg(server, container)
    host = cfg["hosts"][node_rank]

    if not no_sync:
        _sync(server)

    ensure_container(host, cfg)
    exec_in_container(host, cfg["container"], "", interactive=True)


@app.command("exec")
def exec_cmd(
    server: str = typer.Argument(..., help="Server name from ~/.rdev/config.yaml"),
    command: str = typer.Argument(..., help="Command to execute"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    node_rank: int = typer.Option(0, "--node-rank", "-r", help="Node index"),
):
    """Sync + ensure container + execute command."""
    cfg = _resolve_cfg(server, container)
    host = cfg["hosts"][node_rank]

    if not no_sync:
        _sync(server)

    ensure_container(host, cfg)
    exec_in_container(host, cfg["container"], command)


@app.command()
def setup(
    server: str = typer.Argument(..., help="Server name from ~/.rdev/config.yaml"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Create container + run setup on all nodes."""
    cfg = _resolve_cfg(server, container)

    for host in cfg["hosts"]:
        ensure_container(host, cfg)
        run_setup(host, cfg)
