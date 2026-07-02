"""Container lifecycle over SSH. Lifecycle fns take ``Instance``; low-level
helpers (_ssh_run, check_container, ...) stay string-typed (host + name)."""

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from my_toolbox.rdev.topology import Instance
from my_toolbox.ui import ScrollWindow


def _stdout_is_tty() -> bool:
    """True iff our stdout is a real terminal (not piped/redirected).

    The ScrollWindow emits ANSI redraw escapes that only make sense on a TTY;
    non-TTY callers (the Bash tool, CI, `> file`) must get plain pass-through.
    """
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _ssh_run(
    host: str,
    cmd: str,
    *,
    interactive: bool = False,
    stream: bool = False,
    render: bool = True,
    window_desc: Optional[str] = None,
    window_height: int = 8,
) -> subprocess.CompletedProcess:
    """SSH-run a command on `host`.

    interactive=True: allocates a TTY; output goes to terminal (inherited).
    stream=True: long-running command whose output should be shown live.
        When ``render`` is True AND stdout is a TTY, output is captured and
        rendered in a dim fixed-height ScrollWindow (docker build / cargo
        style); ``\r``-redraw progress bars (pip, docker pull) render in place.
        Otherwise (non-TTY, or render=False) it streams straight through.
    Otherwise: stdout/stderr are captured for programmatic inspection.

    Returns a CompletedProcess; stream/interactive callers use only .returncode.
    """
    ssh_cmd = ["ssh"]
    if interactive or stream:
        # `-t` allocates a pseudo-TTY so docker pull / pip can render dynamic
        # progress bars; without it they fall back to line-per-status output.
        ssh_cmd.append("-t")
    ssh_cmd.extend([host, cmd])

    if interactive:
        return subprocess.run(ssh_cmd)

    if stream:
        # stream callers only read .returncode, so wrap the int into a
        # CompletedProcess to keep _ssh_run's return-type contract uniform.
        if not render or not _stdout_is_tty():
            return subprocess.run(ssh_cmd)
        rc = _stream_to_window(ssh_cmd, desc=window_desc, height=window_height)
        return subprocess.CompletedProcess(args=ssh_cmd, returncode=rc)

    return subprocess.run(ssh_cmd, capture_output=True)


def _stream_to_window(
    argv: list[str],
    *,
    desc: Optional[str] = None,
    stdin: Optional[str] = None,
    use_pty: bool = False,
    height: int = 8,
) -> int:
    """Run argv, render merged stdout+stderr through a ScrollWindow, return rc.

    The single source of truth for "stream a subprocess into the window". Two
    capture strategies, chosen by ``use_pty``:

    - ``use_pty=False`` (pipe; the default): captures stdout+stderr via a PIPE.
      Used for setup / pip install / docker pull / bootstrap. If ``stdin`` is
      given (a script body), it is piped to the child and closed BEFORE reading
      stdout, else stdin is inherited.
    - ``use_pty=True`` (exec): captures via a local PTY (``pty.openpty``) so the
      remote keeps seeing a TTY (ssh sees isatty -> color/progress survive),
      with ONLCR post-processing disabled so the window's \\r/\\n semantics
      match the pipe path. stdin is inherited (interactive prompts work);
      ``stdin`` is ignored in this mode.

    Non-TTY fallback: when our stdout isn't a terminal (Bash tool / CI / `> file`),
    run plain inherited-fd pass-through -- ANSI redraw escapes must NOT land in
    captured output. Returns the process exit code either way.
    """
    if not _stdout_is_tty():
        return subprocess.run(argv, input=stdin.encode() if stdin else None).returncode

    if use_pty:
        return _stream_via_pty(argv, desc=desc, height=height)
    return _stream_via_pipe(argv, desc=desc, stdin=stdin, height=height)


def _stream_via_pipe(
    argv: list[str],
    *,
    desc: Optional[str],
    stdin: Optional[str],
    height: int,
) -> int:
    """Pipe capture: Popen(stdout=PIPE, stderr=STDOUT), read text chunks.

    If ``stdin`` (a script body) is given, write it to the child's stdin and
    close that pipe BEFORE reading stdout -- else the child blocks on its stdin
    while we block on its stdout (deadlock).
    """
    # bufsize=1 is genuine line buffering only in text mode; text mode also
    # avoids the RuntimeWarning that binary-mode bufsize=1 emits.
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if stdin:
        assert proc.stdin is not None
        proc.stdin.write(stdin)
        proc.stdin.close()
    with ScrollWindow(height=height, desc=desc) as win:
        assert proc.stdout is not None
        # Fixed-size chunk read (not `for line`) so \r progress redraws update
        # live instead of buffering until a full line; read(64) returns as soon
        # as any data is available, it does not wait for 64 bytes.
        for chunk in iter(lambda: proc.stdout.read(64), ""):
            if chunk:
                win.write(chunk)
    proc.wait()
    return proc.returncode


def _stream_via_pty(argv: list[str], *, desc: Optional[str], height: int) -> int:
    """PTY capture: pty.openpty() with ONLCR off, read bytes from the master.

    The remote keeps seeing a TTY (ssh sees isatty -> color/progress survive).
    ONLCR is disabled so the remote's '\\n' isn't turned into '\\r\\n' on the
    master side (that injected '\\r' would make ScrollWindow.write blank each
    line). stdin is inherited so remote prompts still work.
    """
    import pty
    import termios

    master_fd, slave_fd = pty.openpty()
    try:
        # termios[1] is oflag; clear OPOST (which gates ONLCR translation).
        attrs = termios.tcgetattr(slave_fd)
        attrs[1] &= ~termios.OPOST
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except OSError:
        pass  # not a tty or unsupported -- accept default translation

    proc = subprocess.Popen(
        argv,
        stdin=None,  # inherited -- keep remote prompts working
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)  # child holds its copy; we read from master only
    with ScrollWindow(height=height, desc=desc) as win:
        try:
            while True:
                try:
                    data = os.read(master_fd, 1024)
                except OSError:
                    # master closed (child exited) -> EIO on some platforms
                    break
                if not data:
                    break
                win.write(data.decode("utf-8", errors="replace"))
        finally:
            os.close(master_fd)
    return proc.wait()


def check_container(host: str, container: str) -> str:
    """Return container status: 'running', 'exited', or 'not_found'."""
    result = _ssh_run(
        host,
        f"docker inspect --format '{{{{.State.Status}}}}' {shlex.quote(container)}",
    )
    if result.returncode != 0:
        return "not_found"
    return result.stdout.decode().strip()


@dataclass
class ContainerInfo:
    status: str  # running, exited, not_found, unreachable
    image: Optional[str] = None
    uptime: Optional[str] = None


@dataclass
class GpuProc:
    container: str  # container name or "-" if not in a container
    mem_mb: int


@dataclass
class GpuInfo:
    index: int
    util_pct: int
    mem_used_mb: int
    mem_total_mb: int
    procs: list  # list[GpuProc]


# Remote script: query nvidia-smi GPUs, processes, and map PIDs to containers
_GPU_QUERY_SCRIPT = r"""
nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total \
    --format=csv,noheader,nounits 2>/dev/null | awk '{print "G|" $0}'
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory \
    --format=csv,noheader,nounits 2>/dev/null | awk '{print "P|" $0}'
nvidia-smi --query-gpu=uuid,index --format=csv,noheader 2>/dev/null | awk '{print "U|" $0}'
for pid in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
    pid=$(echo "$pid" | tr -d ' ')
    [ -z "$pid" ] && continue
    cid=$(grep -azPo 'docker[-/]\K[0-9a-f]{64}' /proc/$pid/cgroup 2>/dev/null | head -c 64)
    if [ -n "$cid" ]; then
        name=$(docker inspect --format '{{.Name}}' "$cid" 2>/dev/null | sed 's|^/||')
        echo "C|$pid|${name:--}"
    else
        echo "C|$pid|-"
    fi
done
"""


def fetch_gpu_info(host: str) -> Optional[list]:
    """Fetch GPU stats + per-GPU process/container info from a remote host.

    Returns list[GpuInfo] or None on failure.
    """
    result = _ssh_run(host, _GPU_QUERY_SCRIPT)
    if result.returncode != 0:
        return None

    gpus: dict[int, GpuInfo] = {}
    uuid_to_idx: dict[str, int] = {}
    # proc_raw: list of (pid, gpu_uuid, mem_mb)
    proc_raw: list = []
    pid_to_container: dict[int, str] = {}

    for line in result.stdout.decode().splitlines():
        line = line.strip()
        if not line:
            continue
        kind, _, rest = line.partition("|")
        parts = [p.strip() for p in rest.split(",")]

        if kind == "G" and len(parts) == 4:
            try:
                idx, util, used, total = (int(p) for p in parts)
                gpus[idx] = GpuInfo(idx, util, used, total, [])
            except ValueError:
                continue
        elif kind == "U" and len(parts) == 2:
            uuid, idx_str = parts
            try:
                uuid_to_idx[uuid] = int(idx_str)
            except ValueError:
                continue
        elif kind == "P" and len(parts) == 3:
            try:
                pid = int(parts[0])
                uuid = parts[1]
                mem = int(parts[2])
                proc_raw.append((pid, uuid, mem))
            except ValueError:
                continue
        elif kind == "C":
            c_parts = rest.split("|", 1)
            if len(c_parts) == 2:
                try:
                    pid_to_container[int(c_parts[0])] = c_parts[1]
                except ValueError:
                    continue

    for pid, uuid, mem in proc_raw:
        idx = uuid_to_idx.get(uuid)
        if idx is None or idx not in gpus:
            continue
        container = pid_to_container.get(pid, "-")
        gpus[idx].procs.append(GpuProc(container=container, mem_mb=mem))

    return [gpus[i] for i in sorted(gpus.keys())]


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _parse_inspect_line(line: str) -> Optional[tuple[str, ContainerInfo]]:
    """Parse one docker inspect line: name|status|image|startedAt|finishedAt."""
    parts = line.strip().split("|")
    if len(parts) < 5:
        return None
    name = parts[0].lstrip("/")
    status, image, started_at, finished_at = parts[1], parts[2], parts[3], parts[4]
    now = datetime.now(timezone.utc)
    uptime = None
    try:
        if status == "running" and started_at:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            uptime = _format_duration((now - started).total_seconds())
        elif status == "exited" and finished_at:
            finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            uptime = _format_duration((now - finished).total_seconds()) + " ago"
    except (ValueError, TypeError):
        pass
    return name, ContainerInfo(status=status, image=image, uptime=uptime)


def list_host_containers(
    host: str, name_filter: str
) -> Optional[list[tuple[str, ContainerInfo]]]:
    """List all containers on host whose name matches the substring filter.

    Returns list of (container_name, info) sorted by name.
    Returns None if the host is unreachable.
    """
    fmt = "{{.Name}}|{{.State.Status}}|{{.Config.Image}}|{{.State.StartedAt}}|{{.State.FinishedAt}}"
    cmd = (
        f"names=$(docker ps -a --filter name={shlex.quote(name_filter)} "
        f"--format '{{{{.Names}}}}'); "
        f'[ -z "$names" ] && exit 0; '
        f"docker inspect --format '{fmt}' $names"
    )
    result = _ssh_run(host, cmd)
    if result.returncode != 0:
        return None
    out: list[tuple[str, ContainerInfo]] = []
    for line in result.stdout.decode().splitlines():
        parsed = _parse_inspect_line(line)
        if parsed is not None:
            out.append(parsed)
    return sorted(out, key=lambda x: x[0])


def inspect_container(host: str, container: str) -> ContainerInfo:
    """Get container status, image, and uptime via SSH."""
    fmt = (
        "{{.State.Status}}|{{.Config.Image}}|{{.State.StartedAt}}|{{.State.FinishedAt}}"
    )
    result = _ssh_run(
        host,
        f"docker inspect --format '{fmt}' {shlex.quote(container)}",
    )
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        if "No such object" in stderr or "Error: No such" in stderr:
            return ContainerInfo(status="not_found")
        return ContainerInfo(status="unreachable")

    parts = result.stdout.decode().strip().split("|")
    if len(parts) < 4:
        return ContainerInfo(status="unknown")

    status, image, started_at, finished_at = parts[0], parts[1], parts[2], parts[3]
    now = datetime.now(timezone.utc)

    uptime = None
    try:
        if status == "running" and started_at:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            uptime = _format_duration((now - started).total_seconds())
        elif status == "exited" and finished_at:
            finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            uptime = _format_duration((now - finished).total_seconds()) + " ago"
    except (ValueError, TypeError):
        pass

    return ContainerInfo(status=status, image=image, uptime=uptime)


def create_container(instance: Instance) -> None:
    host = instance.ssh.alias
    spec = instance.container
    host_root = spec.host_root.as_posix()
    mirror_dir = spec.mirror_dir.as_posix()
    cache_dir = (spec.host_root / ".cache").as_posix()

    parts = [
        "docker",
        "run",
        "-itd",
        "--name",
        spec.name,
        "--gpus",
        "all",
        "--shm-size",
        spec.shm_size,
        "--ipc=host",
        "--pid=host",
        "--network=host",
        "--privileged",
        "--ulimit",
        "memlock=-1",
        "--cap-add=SYS_PTRACE",
        "--cap-add=SYS_ADMIN",
        "-w",
        "/root",
        "-v",
        f"{host_root}:/host_root",
        "-v",
        f"{mirror_dir}:/mirror",
        "-v",
        f"{cache_dir}:/root/.cache",
        "-v",
        "/dev/infiniband:/dev/infiniband",
        "-v",
        "/sys/class/infiniband:/sys/class/infiniband",
        spec.image,
        "tail",
        "-f",
        "/dev/null",
    ]

    cmd = " ".join(shlex.quote(p) for p in parts)
    print(f"  [{host}] creating container {spec.name}...")
    result = _ssh_run(host, cmd)
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(f"Failed to create container on {host}: {stderr}")


def run_setup(instance: Instance) -> None:
    host = instance.ssh.alias
    cmd = (
        f"docker exec {shlex.quote(instance.container.name)} "
        f"bash {shlex.quote(instance.setup.setup_script)}"
    )
    print(f"  [{host}] running setup...")
    result = _ssh_run(host, cmd, stream=True, window_desc=f"setup @ {host}")
    if result.returncode != 0:
        raise RuntimeError(f"Setup failed on {host}")


def install_worktree(instance: Instance, worktree: str) -> None:
    """Symlink + pip install the named worktree inside the container."""
    host = instance.ssh.alias
    cmd = (
        f"docker exec {shlex.quote(instance.container.name)} "
        f"bash {shlex.quote(instance.setup.install_worktree_script)} "
        f"{shlex.quote(worktree)}"
    )
    print(f"  [{host}] installing worktree {worktree}...")
    result = _ssh_run(
        host, cmd, stream=True, window_desc=f"pip install {worktree} @ {host}"
    )
    if result.returncode != 0:
        raise RuntimeError(f"install_worktree failed on {host}")


def _docker_action(instance: Instance, action: str, verb: str) -> None:
    host = instance.ssh.alias
    name = instance.container.name
    print(f"  [{host}] {verb} {name}...")
    result = _ssh_run(host, f"docker {action} {shlex.quote(name)}")
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(f"Failed to {action} on {host}: {stderr}")


def _pull_image(host: str, image: str) -> None:
    print(f"  [{host}] pulling {image}...")
    result = _ssh_run(
        host,
        f"docker pull {shlex.quote(image)}",
        stream=True,
        window_desc=f"docker pull {image} @ {host}",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Pull failed on {host}")


def ensure_container(
    instance: Instance,
    *,
    skip_pull: bool = False,
    worktree: str = "sglang",
) -> None:
    """Start if exited, otherwise pull + create + setup + install_worktree."""
    host = instance.ssh.alias
    status = check_container(host, instance.container.name)

    if status == "running":
        return

    if status == "exited":
        _docker_action(instance, "start", "starting")
        return

    if skip_pull:
        print(f"  [{host}] --skip-pull: using local image {instance.container.image}")
    else:
        _pull_image(host, instance.container.image)
    create_container(instance)
    run_setup(instance)
    install_worktree(instance, worktree)


def ensure_container_running(instance: Instance) -> None:
    """Ensure the dev container is running, without creating it.

    Unlike ensure_container, a missing container is an error (not a create) --
    `rdev install` re-installs into an existing container; bring-up belongs to
    `rdev ctr create`. An exited one is started so docker exec can reach it.
    """
    host = instance.ssh.alias
    status = check_container(host, instance.container.name)
    if status == "running":
        return
    if status == "exited":
        _docker_action(instance, "start", "starting")
        return
    name = instance.container.name
    raise RuntimeError(
        f"container {name!r} not found on {host}; run `rdev ctr create` first"
    )


def start_container(instance: Instance) -> None:
    _docker_action(instance, "start", "starting")


def stop_container(instance: Instance) -> None:
    _docker_action(instance, "stop", "stopping")


def restart_container(instance: Instance) -> None:
    _docker_action(instance, "restart", "restarting")


def remove_container(instance: Instance) -> None:
    """``docker rm -f`` — idempotent: no-op (non-zero rc, ignored) if absent."""
    host = instance.ssh.alias
    name = instance.container.name
    print(f"  [{host}] removing {name}...")
    _ssh_run(host, f"docker rm -f {shlex.quote(name)}")


def recreate_container(
    instance: Instance,
    *,
    skip_pull: bool = False,
    worktree: str = "sglang",
) -> None:
    """Remove + pull + create fresh; for image drift or setup re-run."""
    host = instance.ssh.alias
    remove_container(instance)
    if skip_pull:
        print(f"  [{host}] --skip-pull: using local image {instance.container.image}")
    else:
        _pull_image(host, instance.container.image)
    create_container(instance)
    run_setup(instance)
    install_worktree(instance, worktree)


def exec_in_container(
    host: str, container: str, command: str, *, interactive: bool = False
) -> int:
    """Run a command (or interactive shell) inside the container via SSH.

    Returns the process exit code. Non-interactive runs render through a dim
    ScrollWindow (PTY-captured, so remote color/progress survive); interactive
    shells get a full inherited TTY.
    """
    if interactive:
        docker_cmd = f"docker exec -it {shlex.quote(container)} zsh"
        return subprocess.run(["ssh", "-t", host, docker_cmd]).returncode

    docker_cmd = f"docker exec {shlex.quote(container)} bash -c {shlex.quote(command)}"
    return _stream_to_window(
        ["ssh", "-t", host, docker_cmd], desc=f"exec @ {host}", use_pty=True
    )


def exec_direct(host: str, command: str, *, interactive: bool = False) -> int:
    """Run a command (or login shell) plainly over SSH -- for devbox-mode
    instances, where ssh already lands inside the container.

    Returns the process exit code. See exec_in_container for the
    window/interactive split.
    """
    if interactive:
        return subprocess.run(["ssh", "-t", host]).returncode

    return _stream_to_window(
        ["ssh", "-t", host, f"bash -c {shlex.quote(command)}"],
        desc=f"exec @ {host}",
        use_pty=True,
    )


def attach_tmux_direct(host: str, session: str) -> None:
    """Attach to (or create) a persistent tmux session over plain ssh.

    Uses rx's injected tmux (/opt/radixark/bin/tmux) to share the server
    backing the `<host>-tmux` alias; falls back to system tmux if absent.
    """
    rx_tmux = "/opt/radixark/bin/tmux"
    args = f"new-session -AD -s {shlex.quote(session)}"
    cmd = f"if [ -x {rx_tmux} ]; then exec {rx_tmux} {args}; else exec tmux {args}; fi"
    subprocess.run(["ssh", "-t", host, cmd])


def _build_tmux_launch(
    command: str, session: str, log: str, replace: bool, tmux: str = "tmux"
) -> str:
    """Shell snippet launching `command` in a detached tmux session (output to
    `log`), then verifying it came up. `tmux` is the binary expression to use
    (a path, "tmux", or a shell var like "$T").
    """
    # Subshell-wrap so the redirect covers the WHOLE command, not just its last
    # simple command (`a && b > log` would otherwise only capture `b`).
    inner = f"( {command} ) > {shlex.quote(log)} 2>&1"
    # bash -lc so redirects + login PATH (pip-installed tools) behave as usual.
    tmux_cmd = f"bash -lc {shlex.quote(inner)}"
    prefix = (
        f"{tmux} kill-session -t {shlex.quote(session)} 2>/dev/null; "
        if replace
        else ""
    )
    return (
        f"{prefix}{tmux} new-session -d -s {shlex.quote(session)} {tmux_cmd} "
        f"&& {tmux} has-session -t {shlex.quote(session)}"
    )


def tmux_exec_direct(
    host: str, command: str, *, session: str, log: str, replace: bool = False
) -> int:
    """Launch `command` in a detached tmux session on a devbox; returns at once.

    Prefers rx's injected tmux so `rdev tmux <host> -s <session>` can attach to
    the same server; falls back to system tmux.
    """
    rx_tmux = "/opt/radixark/bin/tmux"
    pick = f"T=$([ -x {shlex.quote(rx_tmux)} ] && echo {shlex.quote(rx_tmux)} || echo tmux); "
    launch = pick + _build_tmux_launch(command, session, log, replace, tmux="$T")
    return subprocess.run(["ssh", host, launch]).returncode


def tmux_exec_in_container(
    host: str,
    container: str,
    command: str,
    *,
    session: str,
    log: str,
    replace: bool = False,
) -> int:
    """Launch `command` in a detached tmux session inside the container; returns
    at once (the tmux server outlives the docker exec that spawned it).
    """
    launch = _build_tmux_launch(command, session, log, replace, tmux="tmux")
    docker_cmd = f"docker exec {shlex.quote(container)} bash -c {shlex.quote(launch)}"
    return subprocess.run(["ssh", host, docker_cmd]).returncode


def run_script_direct(host: str, script: str, *, label: str = "script") -> None:
    """Pipe a local script body into `bash -s` on the remote. Used to bootstrap
    devboxes before any code has been synced (no remote paths to rely on).

    Output is rendered through a dim ScrollWindow (this is the long-running
    apt-get/bootstrap path), falling back to plain pass-through on a non-TTY.
    Raises RuntimeError on non-zero exit.
    """
    print(f"  [{host}] running {label}...")
    rc = _stream_to_window(
        ["ssh", host, "bash -s"], desc=f"{label} @ {host}", stdin=script
    )
    if rc != 0:
        raise RuntimeError(f"{label} failed on {host}")


def push_hf_token_direct(host: str) -> bool:
    """Copy the local HF token to the devbox so model downloads authenticate.

    Reads $HF_TOKEN, falling back to ~/.cache/huggingface/token. Skips (returns
    False) if neither is present. Writes to the remote cache token file --
    huggingface_hub reads it with no env/shell setup, and /root/.cache is
    symlinked to persistent /personal/.cache so it survives across acquires.
    Sent over stdin, never argv, to keep the secret out of the ssh command line.
    """
    from pathlib import Path

    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        token_file = Path.home() / ".cache" / "huggingface" / "token"
        token = token_file.read_text().strip() if token_file.exists() else ""
    if not token:
        print(f"  [{host}] no local HF token; skipping")
        return False

    print(f"  [{host}] pushing HF token...")
    remote = (
        "mkdir -p /root/.cache/huggingface && "
        "cat > /root/.cache/huggingface/token && "
        "chmod 600 /root/.cache/huggingface/token"
    )
    result = subprocess.run(["ssh", host, remote], input=token.encode())
    if result.returncode != 0:
        raise RuntimeError(f"HF token push failed on {host}")
    return True


def run_setup_direct(instance: Instance, hf_cache_local: Optional[str] = None) -> None:
    """Devbox counterpart of run_setup: same setup script, plain ssh.

    hf_cache_local: optional devbox-local HF cache dir, passed to setup.sh as
    $1. When set, setup.sh points HF_HOME there (in the shell rc) instead of
    the infra-managed shared gcsfuse cache.
    """
    host = instance.ssh.alias
    print(f"  [{host}] running setup...")
    result = _ssh_run(
        host,
        f"bash {shlex.quote(instance.setup.setup_script)} "
        f"{shlex.quote(hf_cache_local or '')}",
        stream=True,
        window_desc=f"setup @ {host}",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Setup failed on {host}")


def install_worktree_direct(instance: Instance, worktree: str) -> None:
    """Devbox counterpart of install_worktree: same script, plain ssh."""
    host = instance.ssh.alias
    print(f"  [{host}] installing worktree {worktree}...")
    result = _ssh_run(
        host,
        f"bash {shlex.quote(instance.setup.install_worktree_script)} "
        f"{shlex.quote(worktree)}",
        stream=True,
        window_desc=f"pip install {worktree} @ {host}",
    )
    if result.returncode != 0:
        raise RuntimeError(f"install_worktree failed on {host}")


def probe_host(host: str) -> bool:
    """Cheap reachability check: can we ssh in and run `true`?"""
    return _ssh_run(host, "true").returncode == 0
