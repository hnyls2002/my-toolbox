"""Interactive branch pruning — classify and bulk-delete local branches.

Three sections:
1. Not mine — tracking non-origin remotes (fetched for PR review)
2. Mine (merged/gone) — remote deleted or merged into main
3. Mine (active) — unmerged, still in progress

Usage:
    rgit prune              # interactive mode
    rgit prune --dry-run    # preview without deleting
    rgit prune --main master  # use 'master' as base branch
"""

import os
import re
import subprocess
import sys
import termios
import tty
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import typer

from my_toolbox.ui import bold, cyan_text, dim, green_text, red_text, yellow_text

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Category(Enum):
    NOT_MINE = "Not Mine"
    MINE_MERGED = "Mine — Merged / Gone"
    MINE_ACTIVE = "Mine — Active"


@dataclass
class Branch:
    name: str
    commit: str
    tracking: str  # "origin/foo", "user/foo", "(local)"
    status: str  # "gone", "ahead 3, behind 2", ""
    message: str
    is_worktree: bool
    category: Category
    selected: bool = False


# ---------------------------------------------------------------------------
# Branch classification
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(
    r"^([* +])\s+(\S+)\s+([0-9a-f]+)\s+"
    r"(?:\([^)]+\)\s+)?"  # optional worktree path, e.g. (/path/to/wt)
    r"(?:\[([^\]]+)\]\s+)?"  # optional tracking info, e.g. [origin/foo: gone]
    r"(.*)"
)


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip()


def _detect_main_branch() -> str:
    for name in ("main", "master"):
        r = subprocess.run(
            ["git", "rev-parse", "--verify", name],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return name
    return "main"


def _get_merged_into(main: str) -> set[str]:
    out = _git("branch", "--merged", main, "--no-color")
    return {line.strip().lstrip("* +") for line in out.splitlines() if line.strip()}


def classify(main: str) -> dict[Category, list[Branch]]:
    """Parse `git branch -vv` and classify each branch."""
    output = _git("branch", "-vv", "--no-color")
    current = _git("rev-parse", "--abbrev-ref", "HEAD")
    merged = _get_merged_into(main)

    result: dict[Category, list[Branch]] = {c: [] for c in Category}

    for line in output.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue

        marker, name, commit, tracking_raw, message = m.groups()
        if name in (current, main):
            continue

        is_wt = marker == "+"

        tracking = "(local)"
        status = ""
        if tracking_raw:
            # Distinguish real tracking info ("origin/branch: gone") from
            # commit message tags like "[Core]", "[CI]".  Real tracking
            # refs always contain a "/" (e.g. "origin/foo").
            parts = tracking_raw.split(": ", 1)
            ref_candidate = parts[0]
            if "/" in ref_candidate:
                tracking = ref_candidate
                status = parts[1] if len(parts) > 1 else ""
            # else: it's a commit message tag, keep tracking = "(local)"

        # Classify
        if tracking != "(local)" and not tracking.startswith("origin/"):
            cat = Category.NOT_MINE
        elif "gone" in status or name in merged:
            cat = Category.MINE_MERGED
        else:
            cat = Category.MINE_ACTIVE

        result[cat].append(
            Branch(
                name=name,
                commit=commit,
                tracking=tracking,
                status=status,
                message=message.strip(),
                is_worktree=is_wt,
                category=cat,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def _read_key() -> str:
    """Read a single keypress, handling arrow escape sequences."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return {"A": "up", "B": "down"}.get(ch3, "")
            return ""
        if ch == "\r":
            return "enter"
        if ch == " ":
            return "space"
        if ch in ("\x03", "\x1c"):  # Ctrl-C, Ctrl-backslash
            return "quit"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _tracking_display(b: Branch) -> str:
    """Format the tracking info column for display."""
    if b.tracking == "(local)":
        return dim("local")
    if "gone" in b.status:
        return red_text("gone")
    if b.tracking.startswith("origin/"):
        extra = f" {b.status}" if b.status else ""
        return dim(f"origin{extra}")
    # Non-origin remote — show the remote owner name
    return yellow_text(b.tracking.split("/")[0])


# ---------------------------------------------------------------------------
# Display item types (flat list for rendering)
# ---------------------------------------------------------------------------


@dataclass
class _Header:
    category: Category


@dataclass
class _ToggleAll:
    category: Category


@dataclass
class _BranchRow:
    branch: Branch


@dataclass
class _Spacer:
    pass


_Item = _Header | _ToggleAll | _BranchRow | _Spacer

_SECTION_ORDER = [Category.NOT_MINE, Category.MINE_MERGED, Category.MINE_ACTIVE]
_FOOTER_LINES = 3  # blank + status + blank


# ---------------------------------------------------------------------------
# Interactive selector
# ---------------------------------------------------------------------------


class Selector:
    def __init__(self, grouped: dict[Category, list[Branch]]):
        self.grouped = grouped
        self.items: list[_Item] = []
        self.cursor = 0
        self.scroll = 0
        self._build()
        # Move cursor to first selectable item
        if not self._is_selectable(0):
            self._move(1)

    # -- build flat item list -----------------------------------------------

    def _build(self):
        for cat in _SECTION_ORDER:
            branches = self.grouped.get(cat, [])
            if not branches:
                continue
            self.items.append(_Header(cat))
            self.items.append(_ToggleAll(cat))
            for b in branches:
                self.items.append(_BranchRow(b))
            self.items.append(_Spacer())

    # -- navigation ---------------------------------------------------------

    def _is_selectable(self, idx: int) -> bool:
        if idx < 0 or idx >= len(self.items):
            return False
        item = self.items[idx]
        if isinstance(item, _ToggleAll):
            return True
        if isinstance(item, _BranchRow) and not item.branch.is_worktree:
            return True
        return False

    def _move(self, direction: int):
        pos = self.cursor + direction
        while 0 <= pos < len(self.items):
            if self._is_selectable(pos):
                self.cursor = pos
                return
            pos += direction

    def _category_branches(self, cat: Category) -> list[Branch]:
        return [
            it.branch
            for it in self.items
            if isinstance(it, _BranchRow)
            and it.branch.category == cat
            and not it.branch.is_worktree
        ]

    def _cursor_category(self) -> Optional[Category]:
        item = self.items[self.cursor]
        if isinstance(item, _ToggleAll):
            return item.category
        if isinstance(item, _BranchRow):
            return item.branch.category
        return None

    # -- key handling -------------------------------------------------------

    def handle_key(self, key: str) -> Optional[str]:
        if key == "up":
            self._move(-1)
        elif key == "down":
            self._move(1)
        elif key == "space":
            item = self.items[self.cursor]
            if isinstance(item, _BranchRow) and not item.branch.is_worktree:
                item.branch.selected = not item.branch.selected
                self._move(1)
            elif isinstance(item, _ToggleAll):
                self._toggle_section(item.category)
        elif key in ("a", "A"):
            cat = self._cursor_category()
            if cat is not None:
                self._toggle_section(cat)
        elif key == "enter":
            return "confirm"
        elif key in ("q", "quit"):
            return "cancel"
        return None

    def _toggle_section(self, cat: Category):
        branches = self._category_branches(cat)
        if not branches:
            return
        all_sel = all(b.selected for b in branches)
        for b in branches:
            b.selected = not all_sel

    # -- rendering ----------------------------------------------------------

    def render(self, term_height: int) -> str:
        """Return a full screen's worth of rendered text."""
        all_lines = self._render_all_lines()

        # Find which line the cursor is on
        cursor_line = self._cursor_line_index()

        # Available lines for content (reserve footer)
        visible = max(term_height - _FOOTER_LINES, 5)

        # Adjust scroll so cursor stays visible
        if cursor_line < self.scroll:
            self.scroll = cursor_line
        elif cursor_line >= self.scroll + visible:
            self.scroll = cursor_line - visible + 1
        self.scroll = max(0, min(self.scroll, len(all_lines) - visible))

        # Slice visible portion
        window = all_lines[self.scroll : self.scroll + visible]

        # Footer
        total_sel = sum(
            1 for it in self.items if isinstance(it, _BranchRow) and it.branch.selected
        )
        footer = dim(
            f"  ↑↓ Navigate  Space Toggle  a Toggle section"
            f"  Enter Delete ({total_sel})  q Cancel"
        )

        # Scroll indicator
        if self.scroll > 0:
            window[0] = dim("  ↑ more ↑")
        if self.scroll + visible < len(all_lines):
            window[-1] = dim("  ↓ more ↓")

        return "\n".join(window) + "\n\n" + footer

    def _render_all_lines(self) -> list[str]:
        lines: list[str] = []
        max_name = self._max_name_width()

        for i, item in enumerate(self.items):
            is_cur = i == self.cursor

            if isinstance(item, _Header):
                cat = item.category
                branches = self._category_branches(cat)
                total = len(branches)
                sel = sum(1 for b in branches if b.selected)
                wt = sum(
                    1
                    for it in self.items
                    if isinstance(it, _BranchRow)
                    and it.branch.category == cat
                    and it.branch.is_worktree
                )
                suffix = f" +{wt} worktree" if wt else ""
                lines.append("")
                lines.append(
                    f"  {bold(f'━━ {cat.value} ({sel}/{total}){suffix} ━━━━━━━━━━')}"
                )

            elif isinstance(item, _ToggleAll):
                arrow = cyan_text("›") if is_cur else " "
                branches = self._category_branches(item.category)
                all_sel = all(b.selected for b in branches) if branches else False
                check = green_text("✓") if all_sel else " "
                lines.append(
                    f"  {arrow} [{check}] {cyan_text('Select all / Deselect all')}"
                )

            elif isinstance(item, _BranchRow):
                b = item.branch
                if b.is_worktree:
                    lines.append(
                        f"    {dim(f'[w] {b.name:<{max_name}}  (worktree — skip)')}"
                    )
                else:
                    arrow = cyan_text("›") if is_cur else " "
                    check = green_text("✓") if b.selected else " "
                    tracking = _tracking_display(b)
                    lines.append(
                        f"  {arrow} [{check}] {b.name:<{max_name}}  {tracking}"
                        f"  {dim(b.commit[:9])}"
                    )

            elif isinstance(item, _Spacer):
                lines.append("")

        return lines

    def _cursor_line_index(self) -> int:
        """Map self.cursor (item index) to the line index in rendered output."""
        line_idx = 0
        for i, item in enumerate(self.items):
            if i == self.cursor:
                return line_idx
            if isinstance(item, _Header):
                line_idx += 2  # blank line + header
            else:
                line_idx += 1
        return line_idx

    def _max_name_width(self) -> int:
        w = 30
        for it in self.items:
            if isinstance(it, _BranchRow):
                w = max(w, len(it.branch.name))
        return min(w, 55)

    def selected_branches(self) -> list[Branch]:
        return [
            it.branch
            for it in self.items
            if isinstance(it, _BranchRow) and it.branch.selected
        ]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def interactive_prune(main: Optional[str] = None, dry_run: bool = False) -> None:
    if main is None:
        main = _detect_main_branch()

    grouped = classify(main)
    total = sum(len(v) for v in grouped.values())

    if total == 0:
        typer.echo("No branches to prune (only main and current branch remain).")
        return

    selector = Selector(grouped)

    # Enter alternate screen buffer, hide cursor
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()

    action = "cancel"
    try:
        while True:
            term_size = os.get_terminal_size()
            screen = selector.render(term_size.lines)

            sys.stdout.write("\033[H\033[2J")
            sys.stdout.write(screen)
            sys.stdout.flush()

            key = _read_key()
            result = selector.handle_key(key)
            if result in ("confirm", "cancel"):
                action = result
                break
    finally:
        # Restore terminal: show cursor, leave alternate screen
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()

    if action == "cancel":
        typer.echo("Cancelled.")
        return

    to_delete = selector.selected_branches()
    if not to_delete:
        typer.echo("No branches selected.")
        return

    typer.echo(f"\nDeleting {len(to_delete)} branch(es):\n")
    deleted = 0
    for b in to_delete:
        if dry_run:
            typer.echo(f"  {dim('(dry-run)')} would delete {b.name}")
        else:
            r = subprocess.run(
                ["git", "branch", "-D", b.name],
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                typer.echo(f"  {green_text('✓')} {b.name}")
                deleted += 1
            else:
                typer.echo(f"  {red_text('✗')} {b.name}: {r.stderr.strip()}")

    if dry_run:
        typer.echo(
            f"\n{yellow_text('Dry run:')} {len(to_delete)} branch(es) would be deleted."
        )
    elif deleted:
        typer.echo(f"\n{green_text('Done:')} deleted {deleted} branch(es).")
