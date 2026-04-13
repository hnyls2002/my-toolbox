"""Container lifecycle: check, create, exec via SSH."""

import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def _ssh_run(
    host: str, cmd: str, *, interactive: bool = False
) -> subprocess.CompletedProcess:
    ssh_cmd = ["ssh"]
    if interactive:
        ssh_cmd.append("-t")
    ssh_cmd.extend([host, cmd])
    return subprocess.run(ssh_cmd, capture_output=not interactive)


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


def create_container(host: str, cfg: dict) -> None:
    """Create a new container on the remote host."""
    container = cfg["container"]
    image = cfg["image"]
    host_root = cfg["host_root"]
    host_home = os.path.join(host_root, cfg["host_home"])
    cache_dir = os.path.join(host_root, ".cache")
    shm_size = cfg["shm_size"]

    parts = [
        "docker",
        "run",
        "-itd",
        "--name",
        container,
        "--gpus",
        "all",
        "--shm-size",
        shm_size,
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
        image,
        "tail",
        "-f",
        "/dev/null",
    ]

    cmd = " ".join(shlex.quote(p) for p in parts)
    print(f"  [{host}] creating container {container}...")
    result = _ssh_run(host, cmd)
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(f"Failed to create container on {host}: {stderr}")


def run_setup(host: str, cfg: dict) -> None:
    """Run setup script inside the container."""
    container = cfg["container"]
    setup_script = cfg["setup_script"]
    cmd = f"docker exec {shlex.quote(container)} bash {shlex.quote(setup_script)}"
    print(f"  [{host}] running setup...")
    result = _ssh_run(host, cmd)
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(f"Setup failed on {host}: {stderr}")


def _docker_action(host: str, cfg: dict, action: str, verb: str) -> None:
    """Run a single docker action (start/stop/restart/etc.) on the host's container.

    Raises RuntimeError on non-zero exit.
    """
    container = cfg["container"]
    print(f"  [{host}] {verb} {container}...")
    result = _ssh_run(host, f"docker {action} {shlex.quote(container)}")
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(f"Failed to {verb.rstrip('ing')} on {host}: {stderr}")


def _pull_image(host: str, image: str) -> None:
    """Pull an image on the remote host. Raises on failure."""
    print(f"  [{host}] pulling {image}...")
    result = _ssh_run(host, f"docker pull {shlex.quote(image)}")
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(f"Pull failed on {host}: {stderr}")


def ensure_container(host: str, cfg: dict) -> None:
    """Ensure container is running on the host. Create + setup if needed."""
    container = cfg["container"]
    status = check_container(host, container)

    if status == "running":
        return

    if status == "exited":
        _docker_action(host, cfg, "start", "starting")
        return

    # not_found: pull + create + setup
    _pull_image(host, cfg["image"])
    create_container(host, cfg)
    run_setup(host, cfg)


def start_container(host: str, cfg: dict) -> None:
    """docker start an existing container."""
    _docker_action(host, cfg, "start", "starting")


def stop_container(host: str, cfg: dict) -> None:
    """docker stop a running container."""
    _docker_action(host, cfg, "stop", "stopping")


def restart_container(host: str, cfg: dict) -> None:
    """docker restart a container."""
    _docker_action(host, cfg, "restart", "restarting")


def recreate_container(host: str, cfg: dict) -> None:
    """Remove + pull + create fresh. For image drift or setup re-run."""
    container = cfg["container"]
    print(f"  [{host}] removing {container}...")
    # rm -f is idempotent (no-op if container doesn't exist); returncode not checked
    _ssh_run(host, f"docker rm -f {shlex.quote(container)}")
    _pull_image(host, cfg["image"])
    create_container(host, cfg)
    run_setup(host, cfg)


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
