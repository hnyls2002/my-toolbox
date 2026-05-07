"""rdev CLI: unified remote development tool."""

from typing import Callable, Optional

import typer

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
from my_toolbox.rdev.topology import (
    Cluster,
    Topology,
    get_topology,
    unreferenced_ssh_aliases,
    with_overrides,
)

app = typer.Typer(help="Remote development CLI")


# --- Completion helpers ---


def _complete_host(incomplete: str) -> list[str]:
    """Complete ssh aliases (= host names) across all clusters."""
    return [h for h in get_topology().all_aliases if h.startswith(incomplete)]


def _complete_cluster(incomplete: str) -> list[str]:
    return [c for c in get_topology().all_cluster_names if c.startswith(incomplete)]


def _complete_target(incomplete: str) -> list[str]:
    return _complete_cluster(incomplete) + _complete_host(incomplete)


# --- Resolution helpers ---


def _resolve(
    name: str,
    *,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> tuple[Cluster, list[str], bool]:
    """Resolve cluster name or ssh alias.

    Returns (cluster, hosts, is_specific). is_specific=True when user named
    an ssh alias (single-host scope); False when user named a cluster.
    """
    try:
        target = get_topology().resolve(name)
    except KeyError as e:
        raise typer.Exit(str(e))
    cluster = with_overrides(target.cluster, container=container, image=image)
    hosts = [i.ssh.alias for i in target.instances]
    return cluster, hosts, target.is_specific


def _resolve_host(
    name: str,
    *,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> tuple[Cluster, str]:
    """Resolve to a single host. Errors out if user named a cluster."""
    cluster, hosts, is_specific = _resolve(name, container=container, image=image)
    if not is_specific:
        raise typer.Exit(f"Expected a host name, got cluster: {name}")
    return cluster, hosts[0]


def _sync(
    cluster: Cluster,
    hosts: list[str],
    *,
    yes: bool = False,
    quiet: bool = False,
    only_dirs: Optional[list[str]] = None,
    delete: bool = False,
    dry_run: bool = False,
) -> None:
    """Sync code to a list of hosts within a cluster."""
    from my_toolbox.rdev._sync.sync import SyncTool

    SyncTool(
        cluster=cluster,
        hosts=hosts,
        file_or_path=None,
        delete=delete,
        git_repo=False,
        yes=yes,
        quiet=quiet,
        only_dirs=only_dirs,
        dry_run=dry_run,
    ).sync()


@app.command()
def sync(
    target: str = typer.Argument(
        ..., help="Cluster name or ssh alias", autocompletion=_complete_target
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
    """Sync code to remote. Accepts cluster name or single ssh alias."""
    cluster, hosts, _ = _resolve(target)
    only_dirs = [d.strip() for d in only.split(",") if d.strip()] if only else None
    _sync(
        cluster,
        hosts,
        yes=yes,
        quiet=quiet,
        only_dirs=only_dirs,
        delete=delete,
        dry_run=dry_run,
    )


@app.command()
def shell(
    host: str = typer.Argument(..., help="ssh alias", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Attach interactive shell to existing container. No sync, no build/create."""
    cluster, single_host = _resolve_host(host, container=container)
    name = cluster.container.name

    status = check_container(single_host, name)
    if status == "not_found":
        raise typer.Exit(
            f"container {name!r} not found on {single_host}. "
            f"Run `rdev ctr create {host}` first."
        )
    if status == "exited":
        raise typer.Exit(
            f"container {name!r} on {single_host} is stopped. "
            f"Run `rdev ctr start {host}` first."
        )

    exec_in_container(single_host, name, "", interactive=True)


@app.command("exec")
def exec_cmd(
    host: str = typer.Argument(..., help="ssh alias", autocompletion=_complete_host),
    command: str = typer.Argument(..., help="Command to execute"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull when creating new container"
    ),
):
    """Sync + ensure container + execute command on a single host."""
    cluster, single_host = _resolve_host(host, container=container, image=image)

    if not no_sync:
        _sync(cluster, [single_host], yes=True, quiet=True)

    ensure_container(single_host, cluster, skip_pull=skip_pull)
    exec_in_container(single_host, cluster.container.name, command)


# --- Container lifecycle sub-app (rdev ctr ...) ---


ctr_app = typer.Typer(
    help="Container lifecycle: create, start, stop, restart, recreate"
)
app.add_typer(ctr_app, name="ctr")


def _run_on_hosts(
    cluster: Cluster, hosts: list[str], action: Callable[..., None], **kwargs
) -> None:
    """Run ``action(host, cluster, **kwargs)`` for each host. Collect failures.

    Catches Exception so one host's failure doesn't abort the rest.
    KeyboardInterrupt still propagates.
    """
    failures: list[tuple[str, str]] = []
    for host in hosts:
        try:
            action(host, cluster, **kwargs)
        except Exception as e:
            failures.append((host, str(e)))

    if failures:
        for h, msg in failures:
            typer.echo(f"{typer.style('✗', fg=typer.colors.RED)} {h}: {msg}")
        raise typer.Exit(1)


@ctr_app.command("create")
def ctr_create(
    host: str = typer.Argument(..., help="ssh alias", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    worktree: Optional[str] = typer.Option(
        None, "--worktree", help="Worktree name under common_sync/ to install"
    ),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull when creating new container"
    ),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
):
    """Sync code + create container on a single host (skip if already exists)."""
    cluster, single_host = _resolve_host(host, container=container, image=image)
    wt = worktree or cluster.setup.default_worktree
    if not no_sync:
        _sync(cluster, [single_host], yes=True, quiet=True)
    _run_on_hosts(
        cluster, [single_host], ensure_container, skip_pull=skip_pull, worktree=wt
    )


@ctr_app.command("start")
def ctr_start(
    host: str = typer.Argument(..., help="ssh alias", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Start stopped container on a single host."""
    cluster, single_host = _resolve_host(host, container=container)
    _run_on_hosts(cluster, [single_host], start_container)


@ctr_app.command("stop")
def ctr_stop(
    host: str = typer.Argument(..., help="ssh alias", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Stop running container on a single host."""
    cluster, single_host = _resolve_host(host, container=container)
    _run_on_hosts(cluster, [single_host], stop_container)


@ctr_app.command("restart")
def ctr_restart(
    host: str = typer.Argument(..., help="ssh alias", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Restart container on a single host."""
    cluster, single_host = _resolve_host(host, container=container)
    _run_on_hosts(cluster, [single_host], restart_container)


@ctr_app.command("rm")
def ctr_rm(
    host: str = typer.Argument(..., help="ssh alias", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Force-remove container on a single host (docker rm -f, idempotent)."""
    cluster, single_host = _resolve_host(host, container=container)
    _run_on_hosts(cluster, [single_host], remove_container)


@ctr_app.command("recreate")
def ctr_recreate(
    host: str = typer.Argument(..., help="ssh alias", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    worktree: Optional[str] = typer.Option(
        None, "--worktree", help="Worktree name under common_sync/ to install"
    ),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull, reuse local image"
    ),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
):
    """Sync code + remove/recreate container on a single host (for image drift or setup re-run)."""
    cluster, single_host = _resolve_host(host, container=container, image=image)
    wt = worktree or cluster.setup.default_worktree
    if not no_sync:
        _sync(cluster, [single_host], yes=True, quiet=True)
    _run_on_hosts(
        cluster, [single_host], recreate_container, skip_pull=skip_pull, worktree=wt
    )


# --- status ---


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
    target: Optional[str], topo: Topology
) -> list[tuple[Cluster, list[str]]]:
    """Resolve a status target into a list of (cluster, hosts_to_show)."""
    if target is None:
        return [(c, [i.ssh.alias for i in c.instances]) for c in topo.clusters.values()]
    if topo.is_cluster(target):
        c = topo.clusters[target]
        return [(c, [i.ssh.alias for i in c.instances])]
    if topo.is_alias(target):
        cname, inst = topo.by_alias[target]
        return [(topo.clusters[cname], [inst.ssh.alias])]
    typer.echo(
        typer.style(f"Unknown cluster or ssh alias: {target}", fg=typer.colors.RED),
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
def doctor():
    """Print topology + list ssh aliases not referenced by any cluster."""
    topo = get_topology()
    typer.echo(typer.style("=== Clusters ===", bold=True))
    for cname, cluster in topo.clusters.items():
        c = cluster.container
        typer.echo(f"  {typer.style(cname, fg=typer.colors.CYAN)}")
        typer.echo(f"    container: {c.name}  image: {c.image}")
        typer.echo(f"    host_root: {c.host_root}  home_dir: {c.home_dir}")
        typer.echo(f"    sync_target_base: {cluster.sync_target_base}")
        for inst in cluster.instances:
            ssh = inst.ssh
            proxy = f"  via {ssh.proxy_jump}" if ssh.proxy_jump else ""
            typer.echo(
                f"    - {ssh.alias:22} {ssh.user}@{ssh.hostname}:{ssh.port}{proxy}"
            )

    extras = unreferenced_ssh_aliases(topo)
    if extras:
        typer.echo()
        typer.echo(
            typer.style(f"=== ssh aliases not in rdev ({len(extras)}) ===", bold=True)
        )
        for a in extras:
            typer.echo(f"  {a}")


@app.command()
def status(
    target: Optional[str] = typer.Argument(
        None,
        help="Cluster, ssh alias, or omit for all",
        autocompletion=_complete_target,
    ),
    container: Optional[str] = typer.Option(
        None,
        "--container",
        "-c",
        help="Substring filter for container names (default: cluster.status_filter)",
    ),
    gpu: bool = typer.Option(
        False, "--gpu", "-g", help="Show per-GPU utilization + containers"
    ),
):
    """Show container status across hosts.

    Layout: cluster -> host -> container. Each host lists all containers whose
    name contains the filter substring (default: cluster.status_filter).
    """
    topo = get_topology()
    scopes = _resolve_status_scope(target, topo)

    for cluster, hosts in scopes:
        name_filter = container or cluster.status_filter
        typer.echo(typer.style(f"===={cluster.name}====", bold=True))
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
