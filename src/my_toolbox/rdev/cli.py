"""rdev CLI: unified remote development tool."""

from typing import Callable, Optional

import typer

from my_toolbox.rdev.container import (
    ContainerInfo,
    attach_tmux_direct,
    check_container,
    ensure_container,
    ensure_container_running,
    exec_direct,
    exec_in_container,
    fetch_gpu_info,
    install_worktree,
    install_worktree_direct,
    list_host_containers,
    probe_host,
    push_hf_token_direct,
    recreate_container,
    remove_container,
    restart_container,
    run_script_direct,
    run_setup_direct,
    start_container,
    stop_container,
    tmux_exec_direct,
    tmux_exec_in_container,
)
from my_toolbox.rdev.topology import (
    Cluster,
    Instance,
    Topology,
    get_topology,
    unreferenced_hosts,
    with_overrides,
)
from my_toolbox.ui import dim

app = typer.Typer(help="Remote development CLI")


def _complete_host(incomplete: str) -> list[str]:
    return [h for h in get_topology().all_hosts if h.startswith(incomplete)]


def _complete_cluster(incomplete: str) -> list[str]:
    return [c for c in get_topology().all_cluster_names if c.startswith(incomplete)]


def _complete_target(incomplete: str) -> list[str]:
    return _complete_cluster(incomplete) + _complete_host(incomplete)


def _resolve(
    name: str,
    *,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> tuple[list[Instance], bool]:
    """Returns (instances, is_specific=True iff user named a host, not a cluster).

    CLI --container / --image overrides are applied per instance.
    """
    try:
        target = get_topology().resolve(name)
    except KeyError as e:
        raise typer.Exit(str(e))
    instances = [
        with_overrides(i, container=container, image=image) for i in target.instances
    ]
    return instances, target.is_specific


def _resolve_host(
    name: str,
    *,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> Instance:
    """Errors out if user named a cluster instead of a host."""
    instances, is_specific = _resolve(name, container=container, image=image)
    if not is_specific:
        raise typer.Exit(f"Expected a host name, got cluster: {name}")
    return instances[0]


class _OutsideSyncRoot(Exception):
    """Raised by _cwd_checkout_folder when cwd is outside SYNC_ROOT."""


def _cwd_checkout_folder() -> Optional[str]:
    """The checkout folder under common_sync/ that the local cwd sits in.

    Single source of truth for cwd->folder resolution, shared by sync-scope
    and worktree resolution.

    Returns the first path component under sync_root, or None if cwd is at
    the sync_root top level. Raises _OutsideSyncRoot if cwd is outside it.
    """
    from pathlib import Path

    from my_toolbox.rdev._sync.sync_tree import SyncTree

    root = SyncTree().sync_root.resolve()
    cwd = Path.cwd().resolve()
    if cwd == root:
        return None
    try:
        rel = cwd.relative_to(root)
    except ValueError:
        raise _OutsideSyncRoot(f"cwd {cwd} is outside SYNC_ROOT ({root})")
    return rel.parts[0]


def _resolve_sync_scope(all_dirs: bool, only: Optional[str]) -> Optional[list[str]]:
    """Map the --all / --only flags to a sync scope (None == full sync).

    Precedence: --only (explicit dirs) > --all (full) > cwd checkout folder.
    Raises _OutsideSyncRoot when cwd is outside SYNC_ROOT and no --all/--only
    was given -- callers that don't strictly need a sync (exec/install) should
    catch it and skip sync instead of failing.
    """
    if all_dirs and only:
        raise typer.Exit("--all and --only are mutually exclusive.")
    if only:
        return [d.strip() for d in only.split(",") if d.strip()]
    if all_dirs:
        return None
    return _resolve_cwd_scope_or_raise()


def _resolve_cwd_scope_or_raise() -> Optional[list[str]]:
    """Cwd-derived scope, raising _OutsideSyncRoot (not typer.Exit) when cwd is
    outside SYNC_ROOT -- so exec/install can catch it and skip sync, while
    `rdev sync` turns it into a typer.Exit via _default_only_from_cwd."""
    folder = _cwd_checkout_folder()  # raises _OutsideSyncRoot if outside
    return None if folder is None else [folder]


def _sync(
    instances: list[Instance],
    *,
    yes: bool = False,
    quiet: bool = False,
    only_dirs: Optional[list[str]] = None,
    delete: bool = False,
    dry_run: bool = False,
) -> None:
    from my_toolbox.rdev._sync.sync import SyncTool

    SyncTool(
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
    all_dirs: bool = typer.Option(
        False,
        "--all",
        help="full sync of every tracked dir; default (no flag) syncs only the cwd checkout folder.",
    ),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Comma-separated list of subdirs under common_sync/ to sync (e.g. 'my-toolbox,sglang-dsv4'); skips auto-included worktrees and stale-dir cleanup. Overrides the cwd default; mutually exclusive with --all.",
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
    # `rdev sync` with no scope and cwd outside SYNC_ROOT has nothing to sync
    # -- hard error (unlike exec/install, which skip sync in that case).
    try:
        only_dirs = _resolve_sync_scope(all_dirs, only)
    except _OutsideSyncRoot as e:
        raise typer.Exit(
            f"{e}; cd into a folder under it, or pass --only / --all explicitly."
        )
    instances, _ = _resolve(target)
    _sync(
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
    inst = _resolve_host(host, container=container)
    h = inst.ssh.alias
    name = inst.container.name

    if inst.mode == "devbox":
        if container:
            typer.echo(f"  --container ignored for devbox host {h}")
        exec_direct(h, "", interactive=True)
        return

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


@app.command()
def tmux(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    session: str = typer.Option(
        "rx",
        "-s",
        "--session",
        help="tmux session name (default: rx, matching the <host>-tmux alias)",
    ),
):
    """Attach to a persistent tmux session on a devbox (survives ssh / rx-proxy
    disconnects). Default session `rx` matches the rx `<host>-tmux` alias.
    """
    inst = _resolve_host(host)
    if inst.mode != "devbox":
        raise typer.Exit(f"{host} is not a devbox; persistent tmux is rx-only.")
    attach_tmux_direct(inst.ssh.alias, session)


@app.command()
def nvitop(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    cmd: str = typer.Option(
        "nvitop",
        "--cmd",
        help="monitor TUI to run remotely (e.g. 'nvtop', 'watch -n1 nvidia-smi')",
    ),
):
    """Watch nvitop (or another monitor TUI) on a remote host, auto-reconnecting
    when the ssh link drops. Quit with `q` (or Ctrl-C) to stop for real; a
    dropped connection re-launches the TUI by itself.
    """
    import shlex

    from my_toolbox.rdev.watch import watch_remote

    inst = _resolve_host(host, container=container)
    h = inst.ssh.alias

    if inst.mode == "devbox":
        # ssh already lands inside the container; -l picks up the login PATH
        # (pip-installed nvitop often lives outside the non-login default).
        remote_cmd = f"bash -lc {shlex.quote(cmd)}"
    else:
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
        remote_cmd = f"docker exec -it {shlex.quote(name)} bash -lc {shlex.quote(cmd)}"

    raise typer.Exit(watch_remote(h, remote_cmd, label=cmd))


@app.command("exec")
def exec_cmd(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    command: str = typer.Argument(..., help="Command to execute"),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    all_dirs: bool = typer.Option(
        False,
        "--all",
        help="full sync of every tracked dir; default (no flag) syncs only the cwd checkout folder.",
    ),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Comma-separated subdirs under common_sync/ to sync; overrides the cwd default, mutually exclusive with --all.",
    ),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull when creating new container"
    ),
):
    """Sync + ensure container + execute command on a single host."""
    # exec often runs from outside a worktree (you just want to `ls` on the
    # host); being outside SYNC_ROOT must skip sync, not refuse to run. So
    # resolve the scope only when syncing, and swallow the outside-SYNC_ROOT
    # case as a skip (it doesn't affect the command itself).
    if no_sync:
        only_dirs = None
        skip_sync = True
    else:
        try:
            only_dirs = _resolve_sync_scope(all_dirs, only)
            skip_sync = False
        except _OutsideSyncRoot as e:
            only_dirs = None
            skip_sync = True
            typer.echo(f"  {e} -- skipping sync")
    inst = _resolve_host(host, container=container, image=image)

    if not skip_sync:
        _sync([inst], yes=True, quiet=True, only_dirs=only_dirs)

    if inst.mode == "devbox":
        ignored = [
            flag
            for flag, value in [
                ("--container", container),
                ("--image", image),
                ("--skip-pull", skip_pull),
            ]
            if value
        ]
        if ignored:
            typer.echo(
                f"  {', '.join(ignored)} ignored for devbox host {inst.ssh.alias}"
            )
        rc = exec_direct(inst.ssh.alias, command)
        raise typer.Exit(rc)

    ensure_container(inst, skip_pull=skip_pull)
    rc = exec_in_container(inst.ssh.alias, inst.container.name, command)
    raise typer.Exit(rc)


@app.command("tmux-exec")
def tmux_exec_cmd(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    command: str = typer.Argument(
        ..., help="Command to run in a detached tmux session"
    ),
    session: Optional[str] = typer.Option(
        None,
        "-s",
        "--session",
        help="tmux session name (default: rdev-<random>, so concurrent runs never collide)",
    ),
    log: Optional[str] = typer.Option(
        None,
        "--log",
        help="Log file for the command's output (default: /tmp/rdev-tmux-<session>.log)",
    ),
    replace: bool = typer.Option(
        False, "-r", "--replace", help="Kill an existing session of the same name first"
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    image: Optional[str] = typer.Option(None, "--image", help="Override image"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    all_dirs: bool = typer.Option(
        False,
        "--all",
        help="full sync of every tracked dir; default (no flag) syncs only the cwd checkout folder.",
    ),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Comma-separated subdirs under common_sync/ to sync; overrides the cwd default, mutually exclusive with --all.",
    ),
    skip_pull: bool = typer.Option(
        False, "--skip-pull", help="Skip docker pull when creating new container"
    ),
):
    """Sync + ensure container + launch a command in a detached tmux session.

    Unlike `exec` (foreground, dies when the ssh session ends), this returns
    immediately and the command keeps running in tmux -- the right tool for
    long-running remote work (servers, benches). Poll its log with
    `rdev exec <host> "tail -f <log>"`; attach with `rdev tmux <host> -s <name>`.
    """
    import uuid

    session = session or f"rdev-{uuid.uuid4().hex[:8]}"
    log = log or f"/tmp/rdev-tmux-{session}.log"

    # Sync scope resolution mirrors `exec`: running from outside a worktree must
    # skip sync (not refuse), since you may just be launching something on the host.
    if no_sync:
        only_dirs = None
        skip_sync = True
    else:
        try:
            only_dirs = _resolve_sync_scope(all_dirs, only)
            skip_sync = False
        except _OutsideSyncRoot as e:
            only_dirs = None
            skip_sync = True
            typer.echo(f"  {e} -- skipping sync")
    inst = _resolve_host(host, container=container, image=image)

    if not skip_sync:
        _sync([inst], yes=True, quiet=True, only_dirs=only_dirs)

    if inst.mode == "devbox":
        rc = tmux_exec_direct(
            inst.ssh.alias, command, session=session, log=log, replace=replace
        )
        attach_hint = f"rdev tmux {host} -s {session}"
    else:
        ensure_container(inst, skip_pull=skip_pull)
        rc = tmux_exec_in_container(
            inst.ssh.alias,
            inst.container.name,
            command,
            session=session,
            log=log,
            replace=replace,
        )
        attach_hint = f'rdev exec {host} "docker exec -it {inst.container.name} tmux attach -t {session}"'

    if rc == 0:
        typer.echo(f"  launched tmux '{session}' @ {host}")
        typer.echo(dim(f"    log:    {log}"))
        typer.echo(dim(f'    tail:   rdev exec {host} "tail -f {log}"'))
        typer.echo(dim(f"    attach: {attach_hint}"))
    else:
        typer.echo(f"  failed to launch tmux '{session}' @ {host} (rc={rc})")
    raise typer.Exit(rc)


def _default_worktree_from_cwd() -> Optional[str]:
    """The worktree folder under common_sync/ that the local cwd sits in.

    Thin wrapper over _cwd_checkout_folder; rewraps the outside-SYNC_ROOT
    error with the install-specific fix hint (--worktree).
    """
    try:
        return _cwd_checkout_folder()
    except _OutsideSyncRoot as e:
        raise typer.Exit(
            f"{e}; cd into a folder under it, or pass --worktree explicitly."
        )


def _resolve_worktree(explicit: Optional[str]) -> str:
    """Precedence: explicit --worktree > cwd checkout folder.

    Errors if neither is set (cwd at the common_sync top level, or outside
    SYNC_ROOT) -- unlike `exec`/`ctr`, install has no useful default worktree
    to fall back to, so we make the user pick one explicitly.
    """
    if explicit:
        return explicit
    from_cwd = _default_worktree_from_cwd()
    if from_cwd:
        return from_cwd
    # cwd at the common_sync top level: nothing sensible to infer.
    raise typer.Exit(
        "No worktree to install: cwd is at the common_sync top level. "
        "cd into a checkout, or pass --worktree <name>."
    )


def _confirm(yes: bool) -> None:
    """Wait for Enter before proceeding (skip with --yes). Ctrl-C aborts.

    Mirrors the pause `rdev sync` does before its rsync run.
    """
    if yes:
        return
    input(dim("\n  ⏎  Press Enter to install (Ctrl-C to abort)..."))


@app.command()
def install(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    worktree: Optional[str] = typer.Option(
        None,
        "--worktree",
        "-w",
        help="Worktree name under common_sync/ to install (default: the cwd "
        "checkout folder, e.g. sglang-pr-12345).",
    ),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="skip the confirm prompt before installing"
    ),
    all_dirs: bool = typer.Option(
        False,
        "--all",
        help="full sync of every tracked dir; default (no flag) syncs only the cwd checkout folder.",
    ),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Comma-separated subdirs under common_sync/ to sync; overrides the cwd default, mutually exclusive with --all.",
    ),
):
    """Sync + reinstall one worktree's package (editable) on a single host.

    The lightweight counterpart to `rdev ctr recreate`: re-runs
    install_worktree.sh for the named worktree inside the existing container
    (or directly on a devbox) without recreating it. Use this when a worktree
    changes non-Python bits that PYTHONPATH can't swap -- sgl-kernel / C++/CUDA
    AOT code, dependencies, or package metadata.

    The worktree defaults to the checkout folder your local cwd is in
    (override with --worktree); the synced scope follows the same rule as
    `rdev exec`. Prints the command and waits for Enter before installing
    (pass -y to skip).
    """
    # Like exec, install may run from outside a worktree (--worktree given
    # explicitly); being outside SYNC_ROOT skips sync rather than refusing.
    if no_sync:
        only_dirs = None
        skip_sync = True
    else:
        try:
            only_dirs = _resolve_sync_scope(all_dirs, only)
            skip_sync = False
        except _OutsideSyncRoot as e:
            only_dirs = None
            skip_sync = True
            typer.echo(f"  {e} -- skipping sync")
    wt = _resolve_worktree(worktree)
    inst = _resolve_host(host, container=container)

    if not skip_sync:
        _sync([inst], yes=True, quiet=True, only_dirs=only_dirs)

    script = inst.setup.install_worktree_script
    if inst.mode == "devbox":
        if container:
            typer.echo(f"  --container ignored for devbox host {inst.ssh.alias}")
        # Mirror the command install_worktree_direct runs, so the user can
        # confirm what executes -- same dim('$ ...') style as `rdev sync`.
        # -t matches _ssh_run's stream mode (TTY for live pip progress).
        typer.echo(f"\n  {dim(f'$ ssh -t {inst.ssh.alias} bash {script} {wt}')}")
        _confirm(yes)
        try:
            install_worktree_direct(inst, wt)
        except RuntimeError as e:
            raise typer.Exit(str(e))
        return

    typer.echo(
        f"\n  {dim(f'$ ssh -t {inst.ssh.alias} docker exec {inst.container.name} bash {script} {wt}')}"
    )
    _confirm(yes)
    try:
        ensure_container_running(inst)
        install_worktree(inst, wt)
    except RuntimeError as e:
        raise typer.Exit(str(e))


def _dir_under_common_sync(path: str) -> Optional[str]:
    """Extract the checkout-folder name from a /mirror/common_sync/<d>/... path."""
    from pathlib import Path

    parts = Path(path).parts
    if "common_sync" in parts:
        i = parts.index("common_sync")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


@app.command("devbox-init")
def devbox_init(
    host: str = typer.Argument(
        ..., help="devbox name (= ssh alias)", autocompletion=_complete_host
    ),
    worktree: Optional[str] = typer.Option(
        None, "--worktree", help="Worktree name under common_sync/ to install"
    ),
    no_sync: bool = typer.Option(False, "--no-sync", help="Skip code sync"),
    hf_cache_local: Optional[str] = typer.Option(
        None,
        "--hf-cache-local",
        help="Local HF cache dir (e.g. /root/hf_cache); points HF_HOME off the "
        "shared gcsfuse cache. Pass each acquire to persist.",
    ),
):
    """Full setup of a fresh rx devbox -- the devbox counterpart of `rdev ctr create`.

    Steps: rx ssh-config (alias + sshd) -> bootstrap (rsync, zsh login shell,
    /root/.cache -> /personal/.cache) -> push HF token (skipped if absent
    locally) -> code sync -> setup.sh -> install_worktree.sh. Idempotent;
    rerun after each acquire.
    """
    import subprocess
    from pathlib import Path

    # Install/refresh the ssh alias BEFORE topology resolution: on a fresh
    # acquire the alias doesn't exist yet, so the instance was warn-skipped
    # at load time and _resolve_host would not find it.
    typer.echo(f"  [{host}] rx devbox ssh-config...")
    if subprocess.run(["rx", "devbox", "ssh-config", host]).returncode != 0:
        raise typer.Exit(f"rx devbox ssh-config {host} failed")

    if not get_topology().is_host(host):
        raise typer.Exit(
            f"{host} has an ssh alias now, but is not in rdev config; add "
            f"`discover: rx_config` (or `- host: {host}` under instances) to "
            f"a `mode: devbox` cluster (e.g. rx) in ~/.rdev/config.yaml and rerun."
        )
    inst = _resolve_host(host)
    if inst.mode != "devbox":
        raise typer.Exit(
            f"{host} is not a devbox instance (mode: {inst.mode}); "
            f"use `rdev ctr create` for raw hosts."
        )

    import my_toolbox.docker_dev as docker_dev

    bootstrap = Path(docker_dev.__file__).parent / "devbox_bootstrap.sh"
    run_script_direct(inst.ssh.alias, bootstrap.read_text(), label="bootstrap")

    # After bootstrap so /root/.cache -> /personal/.cache symlink is in place.
    push_hf_token_direct(inst.ssh.alias)

    wt = worktree or inst.setup.default_worktree
    if not no_sync:
        # Sync the worktree plus the checkout holding the setup scripts.
        tooldir = _dir_under_common_sync(inst.setup.setup_script)
        only_dirs = [d for d in dict.fromkeys([tooldir, wt]) if d]
        _sync([inst], yes=True, quiet=True, only_dirs=only_dirs)

    run_setup_direct(inst, hf_cache_local=hf_cache_local)
    install_worktree_direct(inst, wt)
    typer.echo(f"  [{host}] devbox ready")


ctr_app = typer.Typer(
    help="Container lifecycle: create, start, stop, restart, recreate"
)
app.add_typer(ctr_app, name="ctr")


def _resolve_ctr_host(
    name: str,
    *,
    container: Optional[str] = None,
    image: Optional[str] = None,
) -> Instance:
    """_resolve_host + reject devbox-mode instances: their container lifecycle
    is managed by `rx devbox` (acquire/release/reprovision), not `rdev ctr`."""
    inst = _resolve_host(name, container=container, image=image)
    if inst.mode == "devbox":
        raise typer.Exit(
            f"{name} is a devbox; manage its lifecycle with `rx devbox` "
            f"(acquire/release/reprovision), not `rdev ctr`."
        )
    return inst


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
    inst = _resolve_ctr_host(host, container=container, image=image)
    wt = worktree or inst.setup.default_worktree
    if not no_sync:
        _sync([inst], yes=True)
    _run_on_instances([inst], ensure_container, skip_pull=skip_pull, worktree=wt)


@ctr_app.command("start")
def ctr_start(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Start stopped container on a single host."""
    inst = _resolve_ctr_host(host, container=container)
    _run_on_instances([inst], start_container)


@ctr_app.command("stop")
def ctr_stop(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Stop running container on a single host."""
    inst = _resolve_ctr_host(host, container=container)
    _run_on_instances([inst], stop_container)


@ctr_app.command("restart")
def ctr_restart(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Restart container on a single host."""
    inst = _resolve_ctr_host(host, container=container)
    _run_on_instances([inst], restart_container)


@ctr_app.command("rm")
def ctr_rm(
    host: str = typer.Argument(..., help="host", autocompletion=_complete_host),
    container: Optional[str] = typer.Option(None, "--container", "-c"),
):
    """Force-remove container on a single host (docker rm -f, idempotent)."""
    inst = _resolve_ctr_host(host, container=container)
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
    inst = _resolve_ctr_host(host, container=container, image=image)
    wt = worktree or inst.setup.default_worktree
    if not no_sync:
        _sync([inst], yes=True)
    _run_on_instances([inst], recreate_container, skip_pull=skip_pull, worktree=wt)


def _print_container_line(name: str, info: ContainerInfo) -> None:
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
) -> list[tuple[Cluster, list[Instance]]]:
    if target is None:
        return [(c, list(c.instances)) for c in topo.clusters.values()]
    if topo.is_cluster(target):
        c = topo.clusters[target]
        return [(c, list(c.instances))]
    if topo.is_host(target):
        # Host may appear in multiple clusters; list under each.
        return [(topo.clusters[cname], [inst]) for cname, inst in topo.by_host[target]]
    typer.echo(
        typer.style(f"Unknown cluster or host: {target}", fg=typer.colors.RED),
        err=True,
    )
    raise typer.Exit(1)


def _print_gpu_info(host: str) -> None:
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


_CONTAINER_FIELDS = ("name", "image", "host_root", "home_dir")


def _spec_diff_fields(spec, base, fields: tuple[str, ...]) -> list[str]:
    if base is None:
        return []
    return [f for f in fields if getattr(spec, f) != getattr(base, f)]


def _override_tag(items: list[str]) -> str:
    return typer.style(f"  (override: {', '.join(items)})", fg=typer.colors.YELLOW)


@app.command()
def doctor():
    """Print topology + list hosts not referenced by any cluster."""
    topo = get_topology()
    base = topo.defaults_container
    typer.echo(typer.style("=== Clusters ===", bold=True))
    for cname, cluster in topo.clusters.items():
        c = cluster.container
        typer.echo(f"  {typer.style(cname, fg=typer.colors.CYAN)}")

        if cluster.mode == "devbox":
            # Container name/image are decided at `rx devbox acquire`, not by
            # rdev config -- printing the merged defaults would be misleading.
            typer.echo(f"    mode: devbox  (container/image managed by `rx devbox`)")
            typer.echo(f"    sync_target_base: {cluster.sync_target_base}")
        else:
            # Cluster-level annotation: cluster.container vs defaults.container.
            # The value sits on the same line, so we tag only the field names.
            line1 = _spec_diff_fields(c, base, ("name", "image"))
            typer.echo(
                f"    container: {c.name}  image: {c.image}"
                + (_override_tag(line1) if line1 else "")
            )
            line2 = _spec_diff_fields(c, base, ("host_root", "home_dir"))
            typer.echo(
                f"    host_root: {c.host_root}  home_dir: {c.home_dir}"
                + (_override_tag(line2) if line2 else "")
            )
            typer.echo(f"    sync_target_base: {cluster.sync_target_base}")

        # Instance-level annotation: instance.container vs cluster.container.
        # Values aren't shown on the instance line, so include field=value pairs.
        for inst in cluster.instances:
            ssh = inst.ssh
            proxy = f"  via {ssh.proxy_jump}" if ssh.proxy_jump else ""
            line = f"    - {ssh.alias:22} {ssh.user}@{ssh.hostname}:{ssh.port}{proxy}"
            if inst.mode != "raw":
                line += typer.style(f"  [{inst.mode}]", fg=typer.colors.CYAN)
            diffs = _spec_diff_fields(inst.container, c, _CONTAINER_FIELDS)
            if diffs:
                items = [f"{f}={getattr(inst.container, f)}" for f in diffs]
                line += _override_tag(items)
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

    for cluster, instances in scopes:
        name_filter = container or cluster.status_filter
        typer.echo(typer.style(f"===={cluster.name}====", bold=True))
        for inst in instances:
            host = inst.ssh.alias
            typer.echo(f"  {host}:")
            if inst.mode == "devbox":
                if probe_host(host):
                    state = typer.style("devbox", fg=typer.colors.GREEN)
                else:
                    state = typer.style("devbox (unreachable)", fg=typer.colors.RED)
                typer.echo(f"\t{state}")
            else:
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
