"""Container lifecycle: check, create, exec via SSH.

Lifecycle functions take (host: str, cluster: Cluster) where `host` is the ssh
alias (= instance.ssh.alias). Low-level helpers (_ssh_run, check_container,
inspect_container, etc.) stay string-typed since they only need ssh + name.
"""

import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from my_toolbox.rdev.topology import Cluster


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


def create_container(host: str, cluster: Cluster) -> None:
    """Create a new container on the remote host."""
    spec = cluster.container
    host_root = spec.host_root.as_posix()
    host_home = spec.host_home.as_posix()
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
        f"{host_home}:/host_home",
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


def run_setup(host: str, cluster: Cluster) -> None:
    """Run setup script inside the container."""
    cmd = (
        f"docker exec {shlex.quote(cluster.container.name)} "
        f"bash {shlex.quote(cluster.setup.setup_script)}"
    )
    print(f"  [{host}] running setup...")
    result = _ssh_run(host, cmd, stream=True)
    if result.returncode != 0:
        raise RuntimeError(f"Setup failed on {host}")


def install_worktree(host: str, cluster: Cluster, worktree: str) -> None:
    """Install a sglang worktree (symlink + pip install) inside the container."""
    cmd = (
        f"docker exec {shlex.quote(cluster.container.name)} "
        f"bash {shlex.quote(cluster.setup.install_worktree_script)} "
        f"{shlex.quote(worktree)}"
    )
    print(f"  [{host}] installing worktree {worktree}...")
    result = _ssh_run(host, cmd, stream=True)
    if result.returncode != 0:
        raise RuntimeError(f"install_worktree failed on {host}")


def _docker_action(host: str, cluster: Cluster, action: str, verb: str) -> None:
    """Run a single docker action (start/stop/restart/etc.) on the host's container."""
    name = cluster.container.name
    print(f"  [{host}] {verb} {name}...")
    result = _ssh_run(host, f"docker {action} {shlex.quote(name)}")
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(f"Failed to {action} on {host}: {stderr}")


def _pull_image(host: str, image: str) -> None:
    """Pull an image on the remote host. Raises on failure."""
    print(f"  [{host}] pulling {image}...")
    result = _ssh_run(host, f"docker pull {shlex.quote(image)}", stream=True)
    if result.returncode != 0:
        raise RuntimeError(f"Pull failed on {host}")


def ensure_container(
    host: str,
    cluster: Cluster,
    *,
    skip_pull: bool = False,
    worktree: str = "sglang",
) -> None:
    """Ensure container is running on the host. Create + setup if needed."""
    status = check_container(host, cluster.container.name)

    if status == "running":
        return

    if status == "exited":
        _docker_action(host, cluster, "start", "starting")
        return

    if skip_pull:
        print(f"  [{host}] --skip-pull: using local image {cluster.container.image}")
    else:
        _pull_image(host, cluster.container.image)
    create_container(host, cluster)
    run_setup(host, cluster)
    install_worktree(host, cluster, worktree)


def start_container(host: str, cluster: Cluster) -> None:
    """docker start an existing container."""
    _docker_action(host, cluster, "start", "starting")


def stop_container(host: str, cluster: Cluster) -> None:
    """docker stop a running container."""
    _docker_action(host, cluster, "stop", "stopping")


def restart_container(host: str, cluster: Cluster) -> None:
    """docker restart a container."""
    _docker_action(host, cluster, "restart", "restarting")


def remove_container(host: str, cluster: Cluster) -> None:
    """Force-remove the container (idempotent: no-op if not present)."""
    name = cluster.container.name
    print(f"  [{host}] removing {name}...")
    _ssh_run(host, f"docker rm -f {shlex.quote(name)}")


def recreate_container(
    host: str,
    cluster: Cluster,
    *,
    skip_pull: bool = False,
    worktree: str = "sglang",
) -> None:
    """Remove + pull + create fresh. For image drift or setup re-run."""
    remove_container(host, cluster)
    if skip_pull:
        print(f"  [{host}] --skip-pull: using local image {cluster.container.image}")
    else:
        _pull_image(host, cluster.container.image)
    create_container(host, cluster)
    run_setup(host, cluster)
    install_worktree(host, cluster, worktree)


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
