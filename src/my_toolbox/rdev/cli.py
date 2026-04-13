"""rdev CLI: unified remote development tool."""

from dataclasses import dataclass
from typing import Callable, Optional

import typer

from my_toolbox.config import rdev_server, rdev_servers
from my_toolbox.rdev.container import (
    ensure_container,
    exec_in_container,
    fetch_gpu_info,
    inspect_container,
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


def _print_host_status(host: str, container: str, show_gpu: bool = False) -> None:
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

    if show_gpu and info.status != "unreachable":
        _print_gpu_info(host)


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
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    gpu: bool = typer.Option(
        False, "--gpu", "-g", help="Show per-GPU utilization + containers"
    ),
):
    """Show container status across hosts."""
    if target is None:
        # all servers
        for server_name in rdev_servers():
            t = _resolve(server_name, container)
            typer.echo(typer.style(server_name, bold=True))
            for host in t.hosts:
                _print_host_status(host, t.cfg["container"], show_gpu=gpu)
        return

    t = _resolve(target, container)
    if t.is_host_specific:
        _print_host_status(t.hosts[0], t.cfg["container"], show_gpu=gpu)
    else:
        typer.echo(typer.style(t.server, bold=True))
        for h in t.hosts:
            _print_host_status(h, t.cfg["container"], show_gpu=gpu)
