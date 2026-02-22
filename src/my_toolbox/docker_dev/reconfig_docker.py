#!/usr/bin/env python3
"""Reconfigure Docker daemon (data-root, storage-driver, optional NVIDIA runtime)."""

import argparse
import dataclasses
import json
import shutil
import subprocess

from my_toolbox.ui import green_text, section_header, yellow_text


@dataclasses.dataclass
class ReconfigOptions:
    data_root: str = "/data/docker-data"
    nvidia_runtime: bool = True
    clean_all: bool = False

    @classmethod
    def from_args(cls, args) -> "ReconfigOptions":
        return cls(**vars(args))


def sudo_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo"] + cmd, **kwargs)


def step_stop_services():
    print(yellow_text("[1/5] Stopping docker and containerd..."))
    sudo_run(["systemctl", "stop", "docker", "docker.socket"], check=False)
    sudo_run(["systemctl", "stop", "containerd"], check=False)


def step_clean_data(opts: ReconfigOptions):
    if opts.clean_all:
        do_clean = True
    else:
        resp = input("Clean all old data (containers, images, cache)? [y/N]: ")
        do_clean = resp.strip().lower() in ("y", "yes")

    if do_clean:
        print(yellow_text("[2/5] Cleaning all old data..."))
        subdirs = ["containers", "overlay2", "image", "network", "buildkit", "tmp"]
        for d in subdirs:
            sudo_run(["rm", "-rf", f"{opts.data_root}/{d}"], check=False)
        sudo_run(["rm", "-rf", "/var/lib/containerd"], check=False)
        print(green_text("   Old data cleaned."))
    else:
        print(yellow_text("[2/5] Skipping clean, keeping existing data."))
        sudo_run(["rm", "-rf", "/var/lib/containerd"], check=False)


def step_create_dirs(opts: ReconfigOptions):
    print(yellow_text("[3/5] Creating data directory..."))
    sudo_run(["mkdir", "-p", opts.data_root], check=True)


def step_write_config(opts: ReconfigOptions):
    print(yellow_text("[4/5] Writing daemon.json..."))

    config: dict = {
        "data-root": opts.data_root,
        "storage-driver": "overlay2",
        "features": {"containerd-snapshotter": False},
    }

    if opts.nvidia_runtime:
        config["runtimes"] = {
            "nvidia": {"args": [], "path": "nvidia-container-runtime"}
        }
        config["default-runtime"] = "nvidia"

    config_json = json.dumps(config, indent=2)
    sudo_run(["mkdir", "-p", "/etc/docker"], check=True)
    sudo_run(
        ["tee", "/etc/docker/daemon.json"],
        input=config_json.encode(),
        stdout=subprocess.DEVNULL,
        check=True,
    )
    print(green_text("   Written to /etc/docker/daemon.json"))


def step_start_services():
    print(yellow_text("[5/5] Starting services..."))
    sudo_run(["systemctl", "start", "containerd"], check=True)
    sudo_run(["systemctl", "start", "docker"], check=True)


def verify():
    print()
    print(section_header("Verification"))
    print()

    print("--- Docker Info ---")
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        low = line.lower()
        if any(k in low for k in ["storage", "snapshotter", "root"]):
            print(line)

    print()
    print("--- Disk Usage ---")
    df_paths = ["/"]
    if shutil.which("df"):
        subprocess.run(["df", "-h"] + df_paths, check=False)

    print()
    print("--- Containers ---")
    subprocess.run(["docker", "ps", "-a"], check=False)

    print()
    print("--- Images ---")
    subprocess.run(["docker", "images"], check=False)


def reconfig_docker(opts: ReconfigOptions):
    print(green_text(section_header("Docker Reconfig")))
    print(f"  data-root:      {opts.data_root}")
    print(f"  nvidia-runtime: {opts.nvidia_runtime}")
    print(f"  clean-all:      {opts.clean_all}")
    print()

    input("Press Enter to stop services...")
    step_stop_services()

    step_clean_data(opts)

    input("Press Enter to create data directory...")
    step_create_dirs(opts)

    input("Press Enter to write daemon.json...")
    step_write_config(opts)

    input("Press Enter to start services...")
    step_start_services()

    verify()

    print()
    print(green_text(section_header("Done")))


def main():
    parser = argparse.ArgumentParser(
        description="Reconfigure Docker daemon (data-root, overlay2, NVIDIA runtime)."
    )
    parser.add_argument(
        "--data-root",
        "-d",
        type=str,
        default=ReconfigOptions.data_root,
        help=f"Docker data root path (default: {ReconfigOptions.data_root})",
    )
    parser.add_argument(
        "--nvidia-runtime",
        action=argparse.BooleanOptionalAction,
        default=ReconfigOptions.nvidia_runtime,
        help=f"Include NVIDIA runtime in config (default: {ReconfigOptions.nvidia_runtime})",
    )
    parser.add_argument(
        "--clean-all",
        action=argparse.BooleanOptionalAction,
        default=ReconfigOptions.clean_all,
        help="Clean all old data without prompting (default: interactive prompt)",
    )
    args = parser.parse_args()

    opts = ReconfigOptions.from_args(args)
    reconfig_docker(opts)


if __name__ == "__main__":
    main()
