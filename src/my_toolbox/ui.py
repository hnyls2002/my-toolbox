import shutil
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
strikethrough = lambda x: f"\x1b[9m{x}\x1b[0m"
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

    @staticmethod
    def clear_line():
        """Erase the entire current line (CSI 2K)."""
        print("\x1b[2K", end="", flush=True)

    @staticmethod
    def carriage_return():
        """Move to column 0 of the current line."""
        print("\r", end="", flush=True)


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


class ScrollWindow:
    """Fixed-height scrolling window (docker build / cargo style).

    Renders the last ``height`` lines of a stream inline (non-alternate-screen):
    new lines push older ones up and off the top, all content dim-styled. A
    ``\r`` (carriage return) resets the *current* line instead of advancing,
    so pip / docker-pull `\r`-redraw progress bars render correctly within one
    row.

    Usage::

        with ScrollWindow(height=8, desc="pip install") as win:
            for chunk in stream:
                win.write(chunk)

    Redraw strategy (portable across terminal emulators -- no scroll-region):
    move cursor up ``height`` rows, then for each row emit carriage-return +
    clear-line + dim(content). Empty buffer rows are cleared blank.

    NOT for non-TTY output: callers must guard (see ``isatty`` check in
    container.py) and fall back to plain pass-through when stdout is piped,
    else ANSI redraw escapes land in captured/CI output.
    """

    def __init__(self, height: int = 8, desc: Optional[str] = None):
        self.height = height
        self.desc = desc
        # committed lines that have scrolled past the current (in-progress) line
        self._lines: list[str] = []
        # the current line being accumulated (before its terminating newline)
        self._cur = ""
        # number of terminal rows physically reserved so far (printed as blank
        # newlines on enter / grown on render). The window grows on demand up to
        # `height`, instead of reserving the full height up front -- so a short
        # command (a few lines of output) doesn't display a big empty frame.
        self._reserved = 0
        # a trailing \r held back when a chunk ends in \r (may be a split \r\n)
        self._pending_cr = False

    @staticmethod
    def _term_width() -> int:
        """Terminal column count (fallback 80 if undetectable).

        Each rendered line is truncated to this so it occupies exactly one
        terminal row -- otherwise wide lines (pip's ~120-char 'Requirement
        already satisfied ...') wrap and break the move_up row math,
        making the window cascade downward.
        """
        try:
            return shutil.get_terminal_size().columns
        except (OSError, ValueError):
            return 80

    def visible_lines(self) -> list[str]:
        """The last ``height`` lines currently in the window (most recent last).

        Single source of truth for what _render shows; also the natural thing to
        unit-test (the buffer state, decoupled from cursor/ANSI rendering).
        Includes the in-progress current line when it has content.
        """
        all_lines = self._lines + ([self._cur] if self._cur else [])
        return all_lines[-self.height :]

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "ScrollWindow":
        CursorTool.hide_cursor()
        if self.desc:
            print(section_header(self.desc))
        # Reserve nothing yet -- the body grows on demand in _render.
        return self

    def __exit__(self, *exc):
        # Final render (only if anything was drawn -- zero output leaves no
        # body, so don't move the cursor at all), then leave the cursor on a
        # fresh line and restore visibility.
        if self._reserved:
            self._render()
        CursorTool.show_cursor()
        sys.stdout.write("\n")
        return False  # do not suppress exceptions

    # -- feed --------------------------------------------------------------

    def write(self, text: str) -> None:
        """Feed a chunk of output into the window and redraw.

        Newline handling distinguishes two cases that both carry a carriage
        return over a PTY:
          - ``\\r\\n`` (newline, incl. PTY/ssh ONLCR-injected \\r): commits the
            current line and advances. The \\r is part of the newline, NOT a
            redraw -- collapsing it to a reset would blank every line.
          - a lone ``\\r`` (no \\n after, as pip/docker-pull emit for in-place
            progress): resets the current line's content (redraw-in-place).

        A trailing ``\\r`` at a chunk boundary is held back (``_pending_cr``)
        until the next chunk arrives, so a ``\\r`` split from its ``\\n``
        across chunks is still treated as a newline, not a stray reset.
        """
        if self._pending_cr:
            text = "\r" + text
            self._pending_cr = False
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\r":
                if i + 1 < n and text[i + 1] == "\n":
                    self._lines.append(self._cur)
                    self._cur = ""
                    i += 2
                    continue
                if i == n - 1:
                    # trailing \r: might be a split \r\n -- defer to next chunk.
                    self._pending_cr = True
                    i += 1
                    continue
                # lone \r mid-chunk: in-place redraw -- reset the current line.
                self._cur = ""
                i += 1
                continue
            if ch == "\n":
                self._lines.append(self._cur)
                self._cur = ""
            else:
                self._cur += ch
            i += 1
        self._render()

    # -- render ------------------------------------------------------------

    def _render(self) -> None:
        """Redraw the last ``height`` lines of the buffer in place, growing the
        window on demand.

        Cursor model: the cursor sits at the "home" row -- one line BELOW the
        currently-reserved body (or at the top, before any rows are reserved).
        We grow `_reserved` up to `height` as content arrives, never reserving
        more than we need so a short command shows a compact frame (not a wall
        of blank rows). Each row: carriage-return + clear-line + dim(content),
        truncated to the terminal width so wide lines don't wrap and desync the
        row math.
        """
        visible = self.visible_lines()
        GUTTER = "┃ "
        gutter = dim(GUTTER)
        width = self._term_width()
        content_width = max(0, width - len(GUTTER))

        target = len(visible)  # rows we want visible now
        if target > self._reserved:
            # Reserve additional rows (capped at height) by printing blank
            # newlines below the current body. The cursor ends up `target`
            # rows below home -- which becomes the new home.
            extra = target - self._reserved
            sys.stdout.write("\n" * extra)
            self._reserved = target
        # Invariant: _reserved == len(visible) here (we only grow it to target).
        # Move from home (1 below the reserved body) up to the top row.
        CursorTool.move_up(self._reserved)
        for i, line in enumerate(visible):
            CursorTool.carriage_return()
            CursorTool.clear_line()
            sys.stdout.write(gutter + dim(line[:content_width]))
            if i < self._reserved - 1:
                CursorTool.move_down(1)
        # Park cursor back at home (1 row below the body).
        CursorTool.move_down(1)
        sys.stdout.flush()


def test():
    with UITool.ui_tool(5) as ui_tool:
        for i in range(20):
            ui_tool.update_line(i % 5, f"line {i % 5}, content {i}")
            time.sleep(0.1)


if __name__ == "__main__":
    test()
