"""Container lifecycle over SSH. Lifecycle fns take ``Instance``; low-level
helpers (_ssh_run, check_container, ...) stay string-typed (host + name)."""

import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from my_toolbox.rdev.topology import Instance


def _ssh_run(
    host: str, cmd: str, *, interactive: bool = False, stream: bool = False
) -> subprocess.CompletedProcess:
    """SSH-run a command on `host`.

    interactive=True: allocates a TTY; output goes to terminal.
    stream=True: output streams to terminal (not captured); use for long-running
        commands like docker pull / pip install where progress is wanted.
    Otherwise: stdout/stderr are captured for programmatic inspection.
    """
    ssh_cmd = ["ssh"]
    if interactive or stream:
        # `-t` allocates a pseudo-TTY so docker pull / pip can render dynamic
        # progress bars; without it they fall back to line-per-status output.
        ssh_cmd.append("-t")
    ssh_cmd.extend([host, cmd])
    capture = not (interactive or stream)
    return subprocess.run(ssh_cmd, capture_output=capture)


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
    result = _ssh_run(host, cmd, stream=True)
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
    result = _ssh_run(host, cmd, stream=True)
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
    result = _ssh_run(host, f"docker pull {shlex.quote(image)}", stream=True)
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
) -> None:
    """Run a command (or interactive shell) inside the container via SSH."""
    if interactive:
        docker_cmd = f"docker exec -it {shlex.quote(container)} zsh"
    else:
        docker_cmd = (
            f"docker exec {shlex.quote(container)} bash -c {shlex.quote(command)}"
        )

    ssh_cmd = ["ssh", "-t", host, docker_cmd]
    subprocess.run(ssh_cmd)


def exec_direct(host: str, command: str, *, interactive: bool = False) -> None:
    """Run a command (or login shell) plainly over SSH -- for devbox-mode
    instances, where ssh already lands inside the container."""
    if interactive:
        ssh_cmd = ["ssh", "-t", host]
    else:
        ssh_cmd = ["ssh", "-t", host, f"bash -c {shlex.quote(command)}"]
    subprocess.run(ssh_cmd)


def run_script_direct(host: str, script: str, *, label: str = "script") -> None:
    """Pipe a local script body into `bash -s` on the remote. Used to bootstrap
    devboxes before any code has been synced (no remote paths to rely on)."""
    print(f"  [{host}] running {label}...")
    result = subprocess.run(["ssh", host, "bash -s"], input=script.encode())
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed on {host}")


def push_hf_token_direct(host: str) -> bool:
    """Copy the local HF token to the devbox so model downloads authenticate.

    Reads $HF_TOKEN, falling back to ~/.cache/huggingface/token. Skips (returns
    False) if neither is present. Writes to the remote cache token file --
    huggingface_hub reads it with no env/shell setup, and /root/.cache is
    symlinked to persistent /personal/.cache so it survives across acquires.
    Sent over stdin, never argv, to keep the secret out of the ssh command line.
    """
    import os
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


def run_setup_direct(
    instance: Instance, hf_cache_local: Optional[str] = None
) -> None:
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
    )
    if result.returncode != 0:
        raise RuntimeError(f"install_worktree failed on {host}")


def probe_host(host: str) -> bool:
    """Cheap reachability check: can we ssh in and run `true`?"""
    return _ssh_run(host, "true").returncode == 0
