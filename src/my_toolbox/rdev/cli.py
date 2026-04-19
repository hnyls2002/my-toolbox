"""rdev CLI: unified remote development tool."""

from dataclasses import dataclass
from typing import Callable, Optional

import typer

from my_toolbox.config import rdev_server, rdev_servers
from my_toolbox.rdev.container import (
    ContainerInfo,
    ensure_container,
    exec_in_container,
    fetch_gpu_info,
    list_host_containers,
    recreate_container,
    restart_container,
    start_container,
    stop_container,
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


@dataclass
class Target:
    """Resolved target: the server group + the hosts to operate on."""

    server: str
    hosts: list[str]  # one host if user specified a host, all if a group
    cfg: dict
    is_host_specific: bool  # True if user passed a host name (not a group)


def _resolve(name: str, container: Optional[str] = None) -> Target:
    """Resolve a server-group or host name into a Target. Raises on unknown."""
    servers = rdev_servers()

    if name in servers:
        cfg = rdev_server(name)
        if container:
            cfg["container"] = container
        return Target(
            server=name,
            hosts=list(cfg["hosts"]),
            cfg=cfg,
            is_host_specific=False,
        )

    for server_name, server_cfg in servers.items():
        if name in server_cfg.get("hosts", []):
            cfg = rdev_server(server_name)
            if container:
                cfg["container"] = container
            return Target(
                server=server_name,
                hosts=[name],
                cfg=cfg,
                is_host_specific=True,
            )

    raise typer.Exit(f"Unknown server or host: {name}")


def _resolve_host(name: str, container: Optional[str] = None) -> Target:
    """Resolve a host name (not a server group). Raises if name is a group or unknown."""
    target = _resolve(name, container)
    if not target.is_host_specific:
        raise typer.Exit(f"Expected a host name, got server group: {name}")
    return target


def _sync(
    server: str,
    hosts: Optional[list[str]] = None,
    yes: bool = False,
    quiet: bool = False,
) -> None:
    """Sync code to remote.

    If hosts is given, sync only to those hosts; otherwise sync to entire group.
    yes=True skips confirmation (used by exec internally).
    quiet=True suppresses verbose progress, only prints final result.
    """
    from my_toolbox.rdev._sync.sync import SyncTool

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


@app.command()
def sync(
    target: str = typer.Argument(
        ...,
        help="Server group or host name",
        autocompletion=_complete_target,
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip confirmation"),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="suppress verbose progress, print final result only",
    ),
):
    """Sync code to remote. Accepts server group or single host."""
    t = _resolve(target)
    _sync(
        t.server,
        hosts=t.hosts if t.is_host_specific else None,
        yes=yes,
        quiet=quiet,
    )


@app.command()
def shell(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Ensure container + interactive shell. No sync."""
    t = _resolve_host(host, container)
    single_host = t.hosts[0]

    ensure_container(single_host, t.cfg)
    exec_in_container(single_host, t.cfg["container"], "", interactive=True)


@app.command("exec")
def exec_cmd(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    command: str = typer.Argument(..., help="Command to execute"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
):
    """Sync cluster group + ensure container + execute command."""
    t = _resolve_host(host, container)
    single_host = t.hosts[0]

    if not no_sync:
        _sync(t.server, yes=True, quiet=True)

    ensure_container(single_host, t.cfg)
    exec_in_container(single_host, t.cfg["container"], command)


# --- Container lifecycle sub-app (rdev ctr ...) ---


ctr_app = typer.Typer(
    help="Container lifecycle: create, start, stop, restart, recreate"
)
app.add_typer(ctr_app, name="ctr")


def _run_on_hosts(target: Target, action: Callable[[str, dict], None]) -> None:
    """Run `action(host, cfg)` for each host in target. Collect failures, exit non-zero if any.

    Catches Exception so one host's failure doesn't abort the rest. KeyboardInterrupt
    still propagates (user-initiated cancel should stop everything).
    """
    failures: list[tuple[str, str]] = []
    for host in target.hosts:
        try:
            action(host, target.cfg)
        except Exception as e:
            failures.append((host, str(e)))

    if failures:
        for h, msg in failures:
            typer.echo(f"{typer.style('✗', fg=typer.colors.RED)} {h}: {msg}")
        raise typer.Exit(1)


@ctr_app.command("create")
def ctr_create(
    target: str = typer.Argument(
        ..., help="Server group or host", autocompletion=_complete_target
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Create container (skip if already exists). Runs setup only on new containers."""
    _run_on_hosts(_resolve(target, container), ensure_container)


@ctr_app.command("start")
def ctr_start(
    target: str = typer.Argument(
        ..., help="Server group or host", autocompletion=_complete_target
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Start stopped container(s)."""
    _run_on_hosts(_resolve(target, container), start_container)


@ctr_app.command("stop")
def ctr_stop(
    target: str = typer.Argument(
        ..., help="Server group or host", autocompletion=_complete_target
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Stop running container(s)."""
    _run_on_hosts(_resolve(target, container), stop_container)


@ctr_app.command("restart")
def ctr_restart(
    target: str = typer.Argument(
        ..., help="Server group or host", autocompletion=_complete_target
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Restart container(s)."""
    _run_on_hosts(_resolve(target, container), restart_container)


@ctr_app.command("recreate")
def ctr_recreate(
    target: str = typer.Argument(
        ..., help="Server group or host", autocompletion=_complete_target
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Remove + pull + create fresh (for image drift or setup re-run)."""
    _run_on_hosts(_resolve(target, container), recreate_container)


def _print_container_line(name: str, info: ContainerInfo) -> None:
    """Print one container's status line (tab-indented under its host)."""
    status_colors = {
        "running": typer.colors.GREEN,
        "exited": typer.colors.YELLOW,
        "not_found": typer.colors.RED,
    }
    color = status_colors.get(info.status, typer.colors.WHITE)
    status_str = typer.style(f"{info.status:<10}", fg=color)
    parts = [f"\t{name:<22}{status_str}"]
    if info.uptime:
        parts.append(f"{info.uptime:<14}")
    if info.image:
        parts.append(info.image)
    typer.echo("".join(parts))


def _resolve_status_scope(
    target: Optional[str], servers: dict
) -> list[tuple[str, list[str]]]:
    """Resolve a status target into a list of (group_name, hosts_to_show).

    - target=None -> all groups, all hosts
    - target=<group>: that group, all its hosts
    - target=<host>: the host's group, only that host
    - Conflicts (same name is both a group and a host, or host in multiple groups)
      raise typer.Exit with a clear error.
    """
    if target is None:
        return [(s, list(c.get("hosts", []))) for s, c in servers.items()]

    is_group = target in servers
    host_groups = [s for s, c in servers.items() if target in c.get("hosts", [])]

    if is_group and host_groups:
        typer.echo(
            typer.style(
                f"Error: '{target}' is both a server group and a host "
                f"(in groups: {host_groups})",
                fg=typer.colors.RED,
            ),
            err=True,
        )
        raise typer.Exit(1)

    if is_group:
        return [(target, list(servers[target].get("hosts", [])))]

    if len(host_groups) > 1:
        typer.echo(
            typer.style(
                f"Warning: host '{target}' belongs to multiple groups "
                f"{host_groups}; listing under each.",
                fg=typer.colors.YELLOW,
            ),
            err=True,
        )
        return [(g, [target]) for g in host_groups]

    if len(host_groups) == 1:
        return [(host_groups[0], [target])]

    typer.echo(
        typer.style(f"Unknown server or host: {target}", fg=typer.colors.RED),
        err=True,
    )
    raise typer.Exit(1)


def _print_gpu_info(host: str) -> None:
    """Print per-GPU stats + container processes."""
    gpus = fetch_gpu_info(host)
    if gpus is None:
        typer.echo(f"    {typer.style('GPU query failed', fg=typer.colors.RED)}")
        return
    if not gpus:
        typer.echo(f"    {typer.style('no GPUs', fg=typer.colors.WHITE)}")
        return

    for gpu in gpus:
        used_gb = gpu.mem_used_mb / 1024
        total_gb = gpu.mem_total_mb / 1024
        util_str = f"{gpu.util_pct:>3}%"
        mem_str = f"{used_gb:>5.1f}G / {total_gb:.0f}G"

        if gpu.procs:
            proc_parts = [f"{p.container}({p.mem_mb/1024:.1f}G)" for p in gpu.procs]
            proc_str = " ".join(proc_parts)
        else:
            proc_str = typer.style("-", fg=typer.colors.BRIGHT_BLACK)

        typer.echo(f"    GPU {gpu.index}   {util_str}   {mem_str}   {proc_str}")


@app.command()
def status(
    target: Optional[str] = typer.Argument(
        None,
        help="Server group, host, or omit for all",
        autocompletion=_complete_target,
    ),
    container: Optional[str] = typer.Option(
        None,
        "--container",
        "-c",
        help="Substring filter for container names (default: host_home from config)",
    ),
    gpu: bool = typer.Option(
        False, "--gpu", "-g", help="Show per-GPU utilization + containers"
    ),
):
    """Show container status across hosts.

    Layout: group -> host -> container. Each host lists all containers whose
    name contains the filter substring (default: the group's ``host_home``).
    """
    servers = rdev_servers()
    scopes = _resolve_status_scope(target, servers)

    for group_name, hosts in scopes:
        group_cfg = rdev_server(group_name)
        name_filter = container or group_cfg.get("host_home", "")
        typer.echo(typer.style(f"===={group_name}====", bold=True))
        for host in hosts:
            typer.echo(f"  {host}:")
            ctrs = list_host_containers(host, name_filter)
            if ctrs is None:
                typer.echo(f"\t{typer.style('unreachable', fg=typer.colors.RED)}")
            elif not ctrs:
                typer.echo(
                    f"\t{typer.style('(no matching containers)', fg=typer.colors.BRIGHT_BLACK)}"
                )
            else:
                for cname, info in ctrs:
                    _print_container_line(cname, info)
            if gpu:
                _print_gpu_info(host)
