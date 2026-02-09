#!/usr/bin/env python3
import argparse
import subprocess

DEFAULT_CONTAINER = "lsyin_sgl"


def main():
    parser = argparse.ArgumentParser(
        description="SSH into a remote host and exec into a Docker container"
    )
    parser.add_argument("host", help="Remote host to SSH into")
    parser.add_argument(
        "--name",
        "-n",
        default=DEFAULT_CONTAINER,
        help=f"Container name (default: {DEFAULT_CONTAINER})",
    )
    args = parser.parse_args()

    cmd = ["ssh", "-t", args.host, f"docker exec -it {args.name} zsh"]
    subprocess.run(cmd)


if __name__ == "__main__":
    main()
