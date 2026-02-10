import sys
import time
from contextlib import contextmanager
from typing import Optional, Union

# Background colors
red_block = lambda x: f"\x1b[41m{x}\x1b[0m"
blue_block = lambda x: f"\x1b[44m{x}\x1b[0m"
yellow_block = lambda x: f"\x1b[43m{x}\x1b[0m"

# Text colors
yellow_text = lambda x: f"\x1b[33m{x}\x1b[0m"
red_text = lambda x: f"\x1b[31m{x}\x1b[0m"
green_text = lambda x: f"\x1b[32m{x}\x1b[0m"
cyan_text = lambda x: f"\x1b[36m{x}\x1b[0m"

# Text styles
bold = lambda x: f"\x1b[1m{x}\x1b[0m"
dim = lambda x: f"\x1b[2m{x}\x1b[0m"
bold_yellow = lambda x: f"\x1b[1;33m{x}\x1b[0m"

HEADER_WIDTH = 40


def format_hosts(hosts: Union[str, list]) -> str:
    """Format host(s) for display: join list, cyan-highlight the result."""
    if isinstance(hosts, str):
        raw = hosts
    elif not hosts:
        raw = "-"
    else:
        raw = ", ".join(str(h) for h in hosts)
    return cyan_text(raw)


def section_header(title: str, width: int = HEADER_WIDTH) -> str:
    """Render a section header like: ━━ Title ━━━━━━━━━━━━━━━━━━"""
    prefix = f"━━ {title} "
    fill = "━" * max(0, width - len(prefix))
    return bold(f"{prefix}{fill}")


def warn_banner(text: str) -> str:
    """Render a warning line like: ⚠  Delete mode enabled"""
    return bold_yellow(f"⚠  {text}")


class CursorTool:
    @staticmethod
    def move_up(n: int):
        print("\x1b[%dA" % n, end="", flush=True)

    @staticmethod
    def move_down(n: int):
        print("\x1b[%dB" % n, end="", flush=True)

    @staticmethod
    def move_right(n: int):
        print("\x1b[%dC" % n, end="", flush=True)

    @staticmethod
    def move_left(n: int):
        print("\x1b[%dD" % n, end="", flush=True)

    @staticmethod
    def move_vertical(n: int):
        if n > 0:
            CursorTool.move_down(n)
        elif n < 0:
            CursorTool.move_up(-n)

    @staticmethod
    def move_horizontal(n: int):
        if n > 0:
            CursorTool.move_right(n)
        elif n < 0:
            CursorTool.move_left(-n)

    @staticmethod
    def reset_line():
        print("\r", end="", flush=True)

    @staticmethod
    def hide_cursor():
        print("\x1b[?25l", end="", flush=True)

    @staticmethod
    def show_cursor():
        print("\x1b[?25h", end="", flush=True)

    @staticmethod
    def clear_screen():
        print("\x1b[2J\x1b[H", end="\n", flush=True)


class UITool:
    def __init__(self, max_lines: int):
        self.max_lines = max_lines
        self.cur_line = 0
        self.cur_col = 0
        self.reset_pos()

        self.line_pos = [0] * max_lines

    def reset_pos(self):
        self.move_cursor(self.max_lines, 0)

    def move_cursor(self, line: Optional[int] = None, col: Optional[int] = None):
        if line is not None:
            CursorTool.move_vertical(line - self.cur_line)
            self.cur_line = line
        if col is not None:
            CursorTool.move_horizontal(col - self.cur_col)
            self.cur_col = col

    def print_char(self, char: str):
        print(char, end="", flush=True)
        self.cur_col += 1

    def print_line(self, content: str):
        print(content, end="", flush=True)
        self.cur_col += len(content)

    def update_char(self, line: int, char: str):
        assert 0 <= line < self.max_lines

        if char in ["\n", "\r"]:
            self.line_pos[line] = 0
        else:
            self.move_cursor(line, self.line_pos[line])
            self.print_char(char)
            self.line_pos[line] = self.cur_col

        self.reset_pos()

    def update_line(self, line: int, content: str):
        assert "\r" not in content and "\n" not in content

        self.move_cursor(line, 0)
        self.print_line(content)
        self.reset_pos()

    def print_desc(self, desc: str):
        header = section_header(desc)
        self.update_line(self.max_lines, header)

    @staticmethod
    @contextmanager
    def ui_tool(max_lines: int, desc: Optional[str] = "Progress"):
        CursorTool.hide_cursor()
        try:
            tool = UITool(max_lines)
            tool.print_desc(desc)
            yield tool
        except Exception as e:
            print(e)
        finally:
            CursorTool.show_cursor()
            sys.stdout.write("\n")


def test():
    with UITool.ui_tool(5) as ui_tool:
        for i in range(20):
            ui_tool.update_line(i % 5, f"line {i % 5}, content {i}")
            time.sleep(0.1)


if __name__ == "__main__":
    test()
