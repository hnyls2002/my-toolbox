"""rdev CLI: unified remote development tool."""

from typing import Optional

import typer

from my_toolbox.config import rdev_server, rdev_servers
from my_toolbox.rdev.container import (
    ensure_container,
    exec_in_container,
    inspect_container,
    run_setup,
)

app = typer.Typer(help="Remote development CLI")


# --- Completion helpers ---


def _complete_host(incomplete: str) -> list[str]:
    """Complete host names from all server groups."""
    hosts = []
    for cfg in rdev_servers().values():
        hosts.extend(cfg.get("hosts", []))
    return [h for h in hosts if h.startswith(incomplete)]


def _complete_server(incomplete: str) -> list[str]:
    """Complete server group names."""
    return [s for s in rdev_servers() if s.startswith(incomplete)]


def _complete_target(incomplete: str) -> list[str]:
    """Complete both server group names and host names."""
    return _complete_server(incomplete) + _complete_host(incomplete)


# --- Resolution helpers ---


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


def _sync(
    server: str,
    hosts: Optional[list[str]] = None,
    yes: bool = False,
    quiet: bool = False,
) -> None:
    """Sync code to remote via lsync.

    If hosts is given, sync only to those hosts; otherwise sync to entire group.
    yes=True skips confirmation (used by exec internally).
    quiet=True suppresses verbose progress, only prints final result.
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
        yes=yes,
        quiet=quiet,
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
        ...,
        help="Server group or host name",
        autocompletion=_complete_target,
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip confirmation"),
):
    """Sync code to remote. Accepts server group or single host."""
    server_name, host = _resolve_target(target)
    if server_name is None:
        raise typer.Exit(f"Unknown server or host: {target}")

    _sync(server_name, hosts=[host] if host else None, yes=yes)


@app.command()
def shell(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Ensure container + interactive shell. No sync."""
    host, _, cfg = _resolve_host(host, container)

    ensure_container(host, cfg)
    exec_in_container(host, cfg["container"], "", interactive=True)


@app.command("exec")
def exec_cmd(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    command: str = typer.Argument(..., help="Command to execute"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
):
    """Sync cluster group + ensure container + execute command."""
    host, server_name, cfg = _resolve_host(host, container)

    if not no_sync:
        _sync(server_name, yes=True, quiet=True)

    ensure_container(host, cfg)
    exec_in_container(host, cfg["container"], command)


@app.command()
def setup(
    server: str = typer.Argument(
        ..., help="Server group name", autocompletion=_complete_server
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Create container + run setup on all nodes in the group."""
    cfg = _resolve_server(server, container)

    for host in cfg["hosts"]:
        ensure_container(host, cfg)
        run_setup(host, cfg)


def _print_host_status(host: str, container: str) -> None:
    """Print status line for a single host."""
    info = inspect_container(host, container)

    status_colors = {
        "running": typer.colors.GREEN,
        "exited": typer.colors.YELLOW,
        "not_found": typer.colors.RED,
        "unreachable": typer.colors.RED,
    }
    color = status_colors.get(info.status, typer.colors.WHITE)
    status_str = typer.style(f"{info.status:<14}", fg=color)

    parts = [f"  {host:<22}{status_str}"]
    if info.uptime:
        parts.append(f"{info.uptime:<12}")
    if info.image:
        parts.append(info.image)

    typer.echo("".join(parts))


@app.command()
def status(
    target: Optional[str] = typer.Argument(
        None,
        help="Server group, host, or omit for all",
        autocompletion=_complete_target,
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Show container status across hosts."""
    servers = rdev_servers()

    if target is None:
        # all servers
        for server_name, server_cfg in servers.items():
            cfg = rdev_server(server_name)
            if container:
                cfg["container"] = container
            typer.echo(typer.style(server_name, bold=True))
            for host in cfg["hosts"]:
                _print_host_status(host, cfg["container"])
        return

    server_name, host = _resolve_target(target)
    if server_name is None:
        raise typer.Exit(f"Unknown server or host: {target}")

    cfg = rdev_server(server_name)
    if container:
        cfg["container"] = container

    if host:
        # single host
        _print_host_status(host, cfg["container"])
    else:
        # entire server group
        typer.echo(typer.style(server_name, bold=True))
        for h in cfg["hosts"]:
            _print_host_status(h, cfg["container"])
