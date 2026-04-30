"""rdev CLI: unified remote development tool."""

from dataclasses import dataclass
from typing import Callable, Optional

import typer

from my_toolbox.config import rdev_server, rdev_servers
from my_toolbox.rdev.container import (
    ContainerInfo,
    check_container,
    ensure_container,
    exec_in_container,
    fetch_gpu_info,
    list_host_containers,
    recreate_container,
    remove_container,
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


def _resolve(
    name: str,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> Target:
    """Resolve a server-group or host name into a Target. Raises on unknown."""
    servers = rdev_servers()

    def _override(cfg: dict) -> dict:
        if container:
            cfg["container"] = container
        if image:
            cfg["image"] = image
        return cfg

    if name in servers:
        cfg = _override(rdev_server(name))
        return Target(
            server=name,
            hosts=list(cfg["hosts"]),
            cfg=cfg,
            is_host_specific=False,
        )

    for server_name, server_cfg in servers.items():
        if name in server_cfg.get("hosts", []):
            cfg = _override(rdev_server(server_name))
            return Target(
                server=server_name,
                hosts=[name],
                cfg=cfg,
                is_host_specific=True,
            )

    raise typer.Exit(f"Unknown server or host: {name}")


def _resolve_host(
    name: str,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> Target:
    """Resolve a host name (not a server group). Raises if name is a group or unknown."""
    target = _resolve(name, container, image)
    if not target.is_host_specific:
        raise typer.Exit(f"Expected a host name, got server group: {name}")
    return target


def _sync(
    server: str,
    hosts: Optional[list[str]] = None,
    yes: bool = False,
    quiet: bool = False,
    only_dirs: Optional[list[str]] = None,
    delete: bool = False,
    dry_run: bool = False,
) -> None:
    """Sync code to remote.

    If hosts is given, sync only to those hosts; otherwise sync to entire group.
    yes=True skips confirmation (used by exec internally).
    quiet=True suppresses verbose progress, only prints final result.
    only_dirs: if set, sync only those subdirectories of common_sync/ (skips
    auto-included worktrees/NDA/git_meta and the remote stale-dir cleanup).
    delete=True passes --delete to rsync and removes stale remote dirs after
    a full sync (mirror mode).
    dry_run=True only previews what would change (rsync --dry-run + lists
    stale top-folders without removing).
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
        delete=delete,
        git_repo=False,
        yes=yes,
        quiet=quiet,
        only_dirs=only_dirs,
        dry_run=dry_run,
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
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Comma-separated list of subdirs under common_sync/ to sync (e.g. 'my-toolbox,sglang-dsv4'); skips auto-included worktrees and stale-dir cleanup.",
    ),
    delete: bool = typer.Option(
        False,
        "--delete",
        "-d",
        help="mirror mode: pass --delete to rsync and remove stale remote dirs after a full sync",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="preview only: pass --dry-run to rsync and list stale top-folders without removing",
    ),
):
    """Sync code to remote. Accepts server group or single host."""
    t = _resolve(target)
    only_dirs = [d.strip() for d in only.split(",") if d.strip()] if only else None
    _sync(
        t.server,
        hosts=t.hosts if t.is_host_specific else None,
        yes=yes,
        quiet=quiet,
        only_dirs=only_dirs,
        delete=delete,
        dry_run=dry_run,
    )


@app.command()
def shell(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Attach interactive shell to existing container. No sync, no build/create."""
    t = _resolve_host(host, container)
    single_host = t.hosts[0]
    ctr = t.cfg["container"]

    status = check_container(single_host, ctr)
    if status == "not_found":
        raise typer.Exit(
            f"container {ctr!r} not found on {single_host}. "
            f"Run `rdev ctr create {host}` first."
        )
    if status == "exited":
        raise typer.Exit(
            f"container {ctr!r} on {single_host} is stopped. "
            f"Run `rdev ctr start {host}` first."
        )

    exec_in_container(single_host, ctr, "", interactive=True)


@app.command("exec")
def exec_cmd(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    command: str = typer.Argument(..., help="Command to execute"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull when creating new container"
    ),
):
    """Sync cluster group + ensure container + execute command."""
    t = _resolve_host(host, container, image)
    single_host = t.hosts[0]

    if not no_sync:
        _sync(t.server, yes=True, quiet=True)

    ensure_container(single_host, t.cfg, skip_pull=skip_pull)
    exec_in_container(single_host, t.cfg["container"], command)


# --- Container lifecycle sub-app (rdev ctr ...) ---


ctr_app = typer.Typer(
    help="Container lifecycle: create, start, stop, restart, recreate"
)
app.add_typer(ctr_app, name="ctr")


def _run_on_hosts(target: Target, action: Callable[..., None], **kwargs) -> None:
    """Run `action(host, cfg, **kwargs)` for each host in target. Collect failures, exit non-zero if any.

    Catches Exception so one host's failure doesn't abort the rest. KeyboardInterrupt
    still propagates (user-initiated cancel should stop everything).
    """
    failures: list[tuple[str, str]] = []
    for host in target.hosts:
        try:
            action(host, target.cfg, **kwargs)
        except Exception as e:
            failures.append((host, str(e)))

    if failures:
        for h, msg in failures:
            typer.echo(f"{typer.style('✗', fg=typer.colors.RED)} {h}: {msg}")
        raise typer.Exit(1)


@ctr_app.command("create")
def ctr_create(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    worktree: str = typer.Option(
        "sglang", "--worktree", help="Worktree name under common_sync/ to install"
    ),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull when creating new container"
    ),
):
    """Create container on a single host (skip if already exists)."""
    _run_on_hosts(
        _resolve_host(host, container, image),
        ensure_container,
        skip_pull=skip_pull,
        worktree=worktree,
    )


@ctr_app.command("start")
def ctr_start(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Start stopped container on a single host."""
    _run_on_hosts(_resolve_host(host, container), start_container)


@ctr_app.command("stop")
def ctr_stop(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Stop running container on a single host."""
    _run_on_hosts(_resolve_host(host, container), stop_container)


@ctr_app.command("restart")
def ctr_restart(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Restart container on a single host."""
    _run_on_hosts(_resolve_host(host, container), restart_container)


@ctr_app.command("rm")
def ctr_rm(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Force-remove container on a single host (docker rm -f, idempotent)."""
    _run_on_hosts(_resolve_host(host, container), remove_container)


@ctr_app.command("recreate")
def ctr_recreate(
    host: str = typer.Argument(..., help="Host name", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    worktree: str = typer.Option(
        "sglang", "--worktree", help="Worktree name under common_sync/ to install"
    ),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull, reuse local image"
    ),
):
    """Remove + pull + create fresh on a single host (for image drift or setup re-run)."""
    _run_on_hosts(
        _resolve_host(host, container, image),
        recreate_container,
        skip_pull=skip_pull,
        worktree=worktree,
    )


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
