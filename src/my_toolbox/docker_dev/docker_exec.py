#!/usr/bin/env python3
import argparse
import subprocess

from my_toolbox.config import rdev_defaults


def main():
    defaults = rdev_defaults()
    container = defaults.get("container", "lsyin_sgl")

    parser = argparse.ArgumentParser(
        description="SSH into a remote host and exec into a Docker container"
    )
    parser.add_argument("host", help="Remote host to SSH into")
    parser.add_argument(
        "--name",
        "-n",
        default=container,
        help=f"Container name (default: {container})",
    )
    args = parser.parse_args()

    cmd = ["ssh", "-t", args.host, f"docker exec -it {args.name} zsh"]
    subprocess.run(cmd)


if __name__ == "__main__":
    main()
