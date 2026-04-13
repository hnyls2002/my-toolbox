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
    host_home = os.path.join(host_root, cfg.get("host_home", "lsyin"))
    cache_dir = os.path.join(host_root, ".cache")
    shm_size = cfg.get("shm_size", "800gb")

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
    setup_script = cfg.get(
        "setup_script",
        "/host_home/common_sync/my-toolbox/src/my_toolbox/docker_dev/setup.sh",
    )
    cmd = f"docker exec {shlex.quote(container)} bash {shlex.quote(setup_script)}"
    print(f"  [{host}] running setup...")
    result = _ssh_run(host, cmd)
    if result.returncode != 0:
        stderr = result.stderr.decode().strip()
        raise RuntimeError(f"Setup failed on {host}: {stderr}")


def ensure_container(host: str, cfg: dict) -> None:
    """Ensure container is running on the host. Create + setup if needed."""
    container = cfg["container"]
    status = check_container(host, container)

    if status == "running":
        return

    if status == "exited":
        print(f"  [{host}] starting stopped container {container}...")
        _ssh_run(host, f"docker start {shlex.quote(container)}")
        return

    # not_found: pull + create + setup
    print(f"  [{host}] pulling {cfg['image']}...")
    _ssh_run(host, f"docker pull {shlex.quote(cfg['image'])}")
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
