"""rdev CLI: unified remote development tool."""

from typing import Optional

import typer

from my_toolbox.config import rdev_server, rdev_servers
from my_toolbox.rdev.container import ensure_container, exec_in_container, run_setup

app = typer.Typer(help="Remote development CLI")


def _resolve_host(host: str, container: Optional[str] = None) -> tuple[str, str, dict]:
    """Resolve a host name to (host, server_name, merged_cfg).

    Looks up which server group the host belongs to.
    """
    servers = rdev_servers()
    for server_name, server_cfg in servers.items():
        hosts = server_cfg.get("hosts", [])
        if host in hosts:
            cfg = rdev_server(server_name)
            if container:
                cfg["container"] = container
            return host, server_name, cfg

    raise typer.Exit(f"Host {host} not found in any server group")


def _resolve_server(server: str, container: Optional[str] = None) -> dict:
    """Load server config, apply container override if given."""
    cfg = rdev_server(server)
    if container:
        cfg["container"] = container
    return cfg


def _sync(server: str, hosts: Optional[list[str]] = None) -> None:
    """Sync code to remote via lsync.

    If hosts is given, sync only to those hosts; otherwise sync to entire group.
    """
    from my_toolbox.lsync.sync import SyncTool

    servers = rdev_servers()
    if server not in servers:
        raise typer.Exit(f"Unknown server: {server}")

    server_config = servers[server]
    if hosts:
        server_config = {**server_config, "hosts": hosts}

    sync_tool = SyncTool(
        server,
        server_config,
        file_or_path=None,
        delete=False,
        git_repo=False,
        yes=True,
    )
    sync_tool.sync()


def _resolve_target(name: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a name to (server_name, host_or_none).

    If name matches a server group, return (server, None).
    If name matches a host, return (server, host).
    """
    servers = rdev_servers()
    if name in servers:
        return name, None
    for server_name, server_cfg in servers.items():
        if name in server_cfg.get("hosts", []):
            return server_name, name
    return None, None


@app.command()
def sync(
    target: str = typer.Argument(
        ..., help="Server group (e.g. rdx-h200) or host (e.g. rdx-h200-3)"
    ),
):
    """Sync code to remote. Accepts server group or single host."""
    server_name, host = _resolve_target(target)
    if server_name is None:
        raise typer.Exit(f"Unknown server or host: {target}")

    _sync(server_name, hosts=[host] if host else None)


@app.command()
def shell(
    host: str = typer.Argument(..., help="Host name (e.g. rdx-h200-3)"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Ensure container + interactive shell. No sync."""
    host, _, cfg = _resolve_host(host, container)

    ensure_container(host, cfg)
    exec_in_container(host, cfg["container"], "", interactive=True)


@app.command("exec")
def exec_cmd(
    host: str = typer.Argument(..., help="Host name (e.g. rdx-h200-3)"),
    command: str = typer.Argument(..., help="Command to execute"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
):
    """Sync cluster group + ensure container + execute command."""
    host, server_name, cfg = _resolve_host(host, container)

    if not no_sync:
        _sync(server_name)

    ensure_container(host, cfg)
    exec_in_container(host, cfg["container"], command)


@app.command()
def setup(
    server: str = typer.Argument(..., help="Server group name (e.g. rdx-h200)"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Create container + run setup on all nodes in the group."""
    cfg = _resolve_server(server, container)

    for host in cfg["hosts"]:
        ensure_container(host, cfg)
        run_setup(host, cfg)
