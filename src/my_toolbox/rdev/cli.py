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
    Instance,
    Topology,
    get_topology,
    unreferenced_hosts,
    with_overrides,
)

app = typer.Typer(help="Remote development CLI")


# --- Completion helpers ---


def _complete_host(incomplete: str) -> list[str]:
    """Complete host names across all clusters."""
    return [h for h in get_topology().all_hosts if h.startswith(incomplete)]


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
) -> tuple[Cluster, list[Instance], bool]:
    """Resolve cluster name or host.

    Returns (cluster, instances, is_specific). is_specific=True when user
    named a host (single-instance scope); False when user named a cluster.
    CLI --container / --image overrides are applied per instance.
    """
    try:
        target = get_topology().resolve(name)
    except KeyError as e:
        raise typer.Exit(str(e))
    instances = [
        with_overrides(i, container=container, image=image) for i in target.instances
    ]
    return target.cluster, instances, target.is_specific


def _resolve_host(
    name: str,
    *,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> tuple[Cluster, Instance]:
    """Resolve to a single instance. Errors out if user named a cluster."""
    cluster, instances, is_specific = _resolve(name, container=container, image=image)
    if not is_specific:
        raise typer.Exit(f"Expected a host name, got cluster: {name}")
    return cluster, instances[0]


def _sync(
    cluster: Cluster,
    instances: list[Instance],
    *,
    yes: bool = False,
    quiet: bool = False,
    only_dirs: Optional[list[str]] = None,
    delete: bool = False,
    dry_run: bool = False,
) -> None:
    """Sync code to a list of instances."""
    from my_toolbox.rdev._sync.sync import SyncTool

    SyncTool(
        cluster_name=cluster.name,
        instances=instances,
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
        ..., help="Cluster name or host", autocompletion=_complete_target
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
    """Sync code to remote. Accepts cluster name or single host."""
    cluster, instances, _ = _resolve(target)
    only_dirs = [d.strip() for d in only.split(",") if d.strip()] if only else None
    _sync(
        cluster,
        instances,
        yes=yes,
        quiet=quiet,
        only_dirs=only_dirs,
        delete=delete,
        dry_run=dry_run,
    )


@app.command()
def shell(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Attach interactive shell to existing container. No sync, no build/create."""
    _, inst = _resolve_host(host, container=container)
    h = inst.ssh.alias
    name = inst.container.name

    status = check_container(h, name)
    if status == "not_found":
        raise typer.Exit(
            f"container {name!r} not found on {h}. "
            f"Run `rdev ctr create {host}` first."
        )
    if status == "exited":
        raise typer.Exit(
            f"container {name!r} on {h} is stopped. "
            f"Run `rdev ctr start {host}` first."
        )

    exec_in_container(h, name, "", interactive=True)


@app.command("exec")
def exec_cmd(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    command: str = typer.Argument(..., help="Command to execute"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull when creating new container"
    ),
):
    """Sync + ensure container + execute command on a single host."""
    cluster, inst = _resolve_host(host, container=container, image=image)

    if not no_sync:
        _sync(cluster, [inst], yes=True, quiet=True)

    ensure_container(inst, skip_pull=skip_pull)
    exec_in_container(inst.ssh.alias, inst.container.name, command)


# --- Container lifecycle sub-app (rdev ctr ...) ---


ctr_app = typer.Typer(
    help="Container lifecycle: create, start, stop, restart, recreate"
)
app.add_typer(ctr_app, name="ctr")


def _run_on_instances(
    instances: list[Instance], action: Callable[..., None], **kwargs
) -> None:
    """Run ``action(instance, **kwargs)`` for each instance. Collect failures.

    Catches Exception so one instance's failure doesn't abort the rest.
    KeyboardInterrupt still propagates.
    """
    failures: list[tuple[str, str]] = []
    for inst in instances:
        try:
            action(inst, **kwargs)
        except Exception as e:
            failures.append((inst.ssh.alias, str(e)))

    if failures:
        for h, msg in failures:
            typer.echo(f"{typer.style('✗', fg=typer.colors.RED)} {h}: {msg}")
        raise typer.Exit(1)


@ctr_app.command("create")
def ctr_create(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
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
    cluster, inst = _resolve_host(host, container=container, image=image)
    wt = worktree or inst.setup.default_worktree
    if not no_sync:
        _sync(cluster, [inst], yes=True, quiet=True)
    _run_on_instances([inst], ensure_container, skip_pull=skip_pull, worktree=wt)


@ctr_app.command("start")
def ctr_start(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Start stopped container on a single host."""
    _, inst = _resolve_host(host, container=container)
    _run_on_instances([inst], start_container)


@ctr_app.command("stop")
def ctr_stop(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Stop running container on a single host."""
    _, inst = _resolve_host(host, container=container)
    _run_on_instances([inst], stop_container)


@ctr_app.command("restart")
def ctr_restart(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Restart container on a single host."""
    _, inst = _resolve_host(host, container=container)
    _run_on_instances([inst], restart_container)


@ctr_app.command("rm")
def ctr_rm(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Force-remove container on a single host (docker rm -f, idempotent)."""
    _, inst = _resolve_host(host, container=container)
    _run_on_instances([inst], remove_container)


@ctr_app.command("recreate")
def ctr_recreate(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
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
    cluster, inst = _resolve_host(host, container=container, image=image)
    wt = worktree or inst.setup.default_worktree
    if not no_sync:
        _sync(cluster, [inst], yes=True, quiet=True)
    _run_on_instances([inst], recreate_container, skip_pull=skip_pull, worktree=wt)


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
    if topo.is_host(target):
        # Host may appear in multiple clusters; list under each.
        return [
            (topo.clusters[cname], [inst.ssh.alias])
            for cname, inst in topo.by_host[target]
        ]
    typer.echo(
        typer.style(f"Unknown cluster or host: {target}", fg=typer.colors.RED),
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


def _override_tag(field_names: list[str]) -> str:
    return typer.style(
        "  (override: " + ", ".join(field_names) + ")", fg=typer.colors.YELLOW
    )


@app.command()
def doctor():
    """Print topology + list hosts not referenced by any cluster."""
    topo = get_topology()
    base = topo.defaults_container
    typer.echo(typer.style("=== Clusters ===", bold=True))
    for cname, cluster in topo.clusters.items():
        c = cluster.container
        typer.echo(f"  {typer.style(cname, fg=typer.colors.CYAN)}")

        # cluster line 1: name + image — annotate which (if any) differ from defaults
        l1_overrides = []
        if base is not None:
            if c.name != base.name:
                l1_overrides.append("name")
            if c.image != base.image:
                l1_overrides.append("image")
        line = f"    container: {c.name}  image: {c.image}"
        if l1_overrides:
            line += _override_tag(l1_overrides)
        typer.echo(line)

        # cluster line 2: host_root + home_dir
        l2_overrides = []
        if base is not None:
            if c.host_root != base.host_root:
                l2_overrides.append("host_root")
            if c.home_dir != base.home_dir:
                l2_overrides.append("home_dir")
        line = f"    host_root: {c.host_root}  home_dir: {c.home_dir}"
        if l2_overrides:
            line += _override_tag(l2_overrides)
        typer.echo(line)

        typer.echo(f"    sync_target_base: {cluster.sync_target_base}")

        for inst in cluster.instances:
            ssh = inst.ssh
            proxy = f"  via {ssh.proxy_jump}" if ssh.proxy_jump else ""
            line = f"    - {ssh.alias:22} {ssh.user}@{ssh.hostname}:{ssh.port}{proxy}"
            # instance-level override = instance.container differs from cluster.container
            inst_overrides = []
            if inst.container.name != c.name:
                inst_overrides.append(f"container.name={inst.container.name}")
            if inst.container.image != c.image:
                inst_overrides.append(f"image={inst.container.image}")
            if inst.container.host_root != c.host_root:
                inst_overrides.append(f"host_root={inst.container.host_root}")
            if inst.container.home_dir != c.home_dir:
                inst_overrides.append(f"home_dir={inst.container.home_dir}")
            if inst_overrides:
                line += "  " + typer.style(
                    "(override: " + ", ".join(inst_overrides) + ")",
                    fg=typer.colors.YELLOW,
                )
            typer.echo(line)

    extras = unreferenced_hosts(topo)
    if extras:
        typer.echo()
        typer.echo(typer.style(f"=== hosts not in rdev ({len(extras)}) ===", bold=True))
        for a in extras:
            typer.echo(f"  {a}")


@app.command()
def status(
    target: Optional[str] = typer.Argument(
        None,
        help="Cluster, host, or omit for all",
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
