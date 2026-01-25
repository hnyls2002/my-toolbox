import os
import subprocess
import threading

from ui import red_block, red_text


def get_lsync_dir() -> str:
    lsync_dir = os.environ.get("LSYNC_DIR", None)
    if lsync_dir is None:
        raise RuntimeError("Please set LSYNC_DIR environment variable")
    return lsync_dir


def popen_with_error_check(command: list[str], allow_exit: bool = True):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def _run_and_check():
        process.wait()

        if not allow_exit or process.returncode != 0:
            cmd_str = " ".join(command)
            print(red_block(cmd_str))
            stderr_content = process.stderr.read() if process.stderr else ""
            print(red_text(stderr_content))
            raise RuntimeError()

    t = threading.Thread(target=_run_and_check)
    t.start()
    return process
