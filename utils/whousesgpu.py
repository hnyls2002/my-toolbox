#!/usr/bin/env python3
import argparse
import dataclasses
import os
import time


@dataclasses.dataclass
class Args:
    watch: bool = False

    @classmethod
    def from_args(cls):
        parser = argparse.ArgumentParser()
        parser.add_argument("--watch", action="store_true")
        parsed_args = parser.parse_args()
        return cls(**vars(parsed_args))


def fetch_gpu_processes(gpu_id: int):
    command = f"nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader --id={gpu_id}"
    with os.popen(command) as f:
        lines = f.readlines()

    infos = [line.strip().split(",") for line in lines if line.strip()]
    infos = [(int(pid_str), mem_str.strip()) for pid_str, mem_str in infos]

    return infos


def get_docker_name(pid: int):
    command = f"grep -azPo 'docker-\\K[0-9a-f]{{64}}' /proc/{pid}/cgroup | xargs -r -0 docker inspect --format '{{{{.Name}}}}'"
    with os.popen(command) as f:
        name = f.read().strip()
    return name


def get_message():
    lines = []
    for gpu_id in range(8):
        infos = fetch_gpu_processes(gpu_id)
        for pid, used_mem in infos:
            name = get_docker_name(pid)
            msg = f"GPU {gpu_id} | PID {pid} | MEM {used_mem} | DOCKER {name}"
            lines.append((gpu_id, msg))

    msg_len = max(len(msg) for _, msg in lines) if lines else 40
    msgs = ["=" * msg_len]
    for i, (gpu_id, msg) in enumerate(lines):
        if i > 0 and gpu_id != lines[i - 1][0]:
            msgs.append("-" * msg_len)
        msgs.append(msg)
    msgs.append("=" * msg_len)
    return "\n".join(msgs)


def main():
    args = Args.from_args()
    if not args.watch:
        print(get_message())
    else:
        try:
            while True:
                os.system("clear")
                print(get_message())
                time.sleep(2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
