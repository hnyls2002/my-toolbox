#!/usr/bin/env python3
import argparse
import dataclasses
import os
import subprocess
from typing import List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETUP_SCRIPT = os.path.join(SCRIPT_DIR, "setup.sh")

DEFAULT_MOUNT_DIRS = [
    "/dev/infiniband:/dev/infiniband",
    "/sys/class/infiniband:/sys/class/infiniband",
]

HOST_HOME_FOLDER = "lsyin"
HOST_CACHE_FOLDER = ".cache"


@dataclasses.dataclass
class DockerConfig:
    host_root: str
    host_home: Optional[str] = None
    cache_dir: Optional[str] = None

    image: str = "lmsysorg/sglang:dev"
    name: str = "lsyin_sgl"
    shm_size: str = "800gb"
    docker_cmd: str = "docker"
    env_vars: List[str] = dataclasses.field(default_factory=list)
    extra_mnt_dirs: List[str] = dataclasses.field(default_factory=list)

    # actions
    pull: bool = True
    setup: bool = True

    @classmethod
    def from_args(cls, args) -> "DockerConfig":
        ret = cls(**vars(args))
        ret.host_home = os.path.join(ret.host_root, HOST_HOME_FOLDER)
        ret.cache_dir = os.path.join(ret.host_root, HOST_CACHE_FOLDER)
        return ret

    def pretty_print(self):
        mnt_dirs = DEFAULT_MOUNT_DIRS + self.extra_mnt_dirs
        print(
            f"DockerConfig:\n"
            f"  host_root: {self.host_root}\n"
            f"  host_home: {self.host_home}\n"
            f"  cache_dir: {self.cache_dir}\n"
            f"  image: {self.image}\n"
            f"  name: {self.name}\n"
            f"  shm_size: {self.shm_size}\n"
            f"  docker_cmd: {self.docker_cmd}\n"
            f"  mnt_dirs: {mnt_dirs}\n"
            f"  --------------actions--------------\n"
            f"  pull: {self.pull}\n"
            f"  setup: {self.setup}\n"
        )


def run_docker(cfg: DockerConfig):
    cfg.pretty_print()

    if cfg.pull:
        # pull docker image first
        input("Press Enter to pull the docker image...")
        run_docker_cmd = [cfg.docker_cmd, "pull", cfg.image]
        subprocess.run(run_docker_cmd, check=True)

    run_docker_cmd = [
        cfg.docker_cmd,
        "run",
        # nerdctl does not support -itd
        "-itd" if cfg.docker_cmd == "docker" else "-td",
        "--name",
        cfg.name,
        "--gpus",
        "all",
        "--shm-size",
        cfg.shm_size,
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
    ]

    run_docker_cmd.extend(["-v", f"{cfg.host_home}:/host_home"])
    run_docker_cmd.extend(["-v", f"{cfg.cache_dir}:/root/.cache"])
    for mount_dir in DEFAULT_MOUNT_DIRS + cfg.extra_mnt_dirs:
        run_docker_cmd.extend(["-v", f"{mount_dir}"])

    for env_var in cfg.env_vars:
        run_docker_cmd.extend(["-e", f"{env_var}"])

    run_docker_cmd.extend([cfg.image, "tail", "-f", "/dev/null"])

    print(" ".join(run_docker_cmd))

    input("Press Enter to continue...")

    subprocess.run(run_docker_cmd, check=True)

    if cfg.setup:
        print(f"Running setup script: {SETUP_SCRIPT}")
        with open(SETUP_SCRIPT, "r") as f:
            subprocess.run(
                [cfg.docker_cmd, "exec", "-i", cfg.name, "bash"],
                stdin=f,
                check=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=str, default=DockerConfig.image)
    parser.add_argument("--name", type=str, default=DockerConfig.name)
    parser.add_argument("--host-root", "-H", type=str, required=True)
    parser.add_argument("--extra-mnt-dirs", "-v", action="append", default=[])
    parser.add_argument("--env-vars", "-e", action="append", default=[])
    parser.add_argument(
        "--pull",
        action=argparse.BooleanOptionalAction,
        default=DockerConfig.pull,
        help=f"Pull the docker image (default: {DockerConfig.pull})",
    )
    parser.add_argument(
        "--setup",
        action=argparse.BooleanOptionalAction,
        default=DockerConfig.setup,
        help=f"Run setup.sh after starting the container (default: {DockerConfig.setup})",
    )
    args = parser.parse_args()

    config = DockerConfig.from_args(args)
    run_docker(config)
