import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from ui import blue_block

LOG_FILE = Path.home() / ".lsync.log"


class LogItem:
    def __init__(
        self,
        now_str: str,
        path: str,
        hosts: str,
        delete: bool,
        git_repo: bool,
    ):
        self.now_str = now_str
        self.path = path
        self.hosts = hosts
        self.delete = delete
        self.git_repo = git_repo

    def to_json(self):
        return json.dumps(self.__dict__)

    @staticmethod
    def from_json(json_str: str):
        return LogItem(**json.loads(json_str))

    def print(self):
        log_str = blue_block(
            f"last syncing: {self.now_str}: {self.path} -> {self.hosts}"
        )
        print(f"last syncing: {log_str}")

    def pretty_verbose(self):
        self.print()
        print(f"delete: {'Yes' if self.delete else 'No'}")
        print(f"git repo: {'Yes' if self.git_repo else 'No'}")


class Logger:
    def __init__(self):
        self.log_file = LOG_FILE
        self.log_file.touch(exist_ok=True)

    def read_last_sync_log(self) -> Optional[LogItem]:
        with self.log_file.open("r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
            return LogItem.from_json(lines[-1]) if lines else None

    def print_last_log(self):
        last_log = self.read_last_sync_log()
        last_log.print() if last_log else print("No last syncing log")

    def log_one(
        self,
        path: Union[str, Path],
        hosts: str,
        delete: bool,
        git_repo: bool,
    ):
        path = path.as_posix() if isinstance(path, Path) else path
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_item = LogItem(now_str, path, hosts, delete, git_repo)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(log_item.to_json() + "\n")
