"""Interactive branch pruning — classify and bulk-delete local + remote branches.

Four sections:
1. Not mine — tracking non-origin remotes (fetched for PR review)
2. Mine (merged/gone) — remote deleted or merged into main
3. Mine (active) — unmerged, still in progress
4. Remote stale — my branches on origin with no local counterpart

Usage:
    rgit prune                  # interactive mode
    rgit prune --dry-run        # preview without deleting
    rgit prune --main master    # use 'master' as base branch
    rgit prune --remote-prefix lsyin  # override auto-detected prefix
"""

import os
import re
import subprocess
import sys
import termios
import tty
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
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
    REMOTE_STALE = "Remote Stale (origin)"


@dataclass
class Branch:
    name: str
    commit: str
    tracking: str  # "origin/foo", "user/foo", "(local)"
    status: str  # "gone", "ahead 3, behind 2", ""
    message: str
    is_worktree: bool
    category: Category
    is_merged: bool = False  # True if merged into main
    is_remote_only: bool = False  # True for remote-only branches
    selected: bool = False

    @property
    def origin_ref(self) -> Optional[str]:
        """Return the branch name on origin (without 'origin/' prefix), if any."""
        if self.is_remote_only:
            return self.name  # already stored without origin/ prefix
        if self.tracking.startswith("origin/") and "gone" not in self.status:
            return self.tracking.removeprefix("origin/")
        return None


# ---------------------------------------------------------------------------
# Branch classification
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(
    r"^([* +])\s+(\S+)\s+([0-9a-f]+)\s+"
    r"(?:\([^)]+\)\s+)?"  # optional worktree path, e.g. (/path/to/wt)
    r"(?:\[([^\]]+)\]\s+)?"  # optional tracking info, e.g. [origin/foo: gone]
    r"(.*)"
)

_REMOTE_LINE_RE = re.compile(r"^\s+origin/(\S+)\s+([0-9a-f]+)\s+(.*)")


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


def _detect_user_prefix(local_branches: list[Branch]) -> Optional[str]:
    """Infer the user's branch naming prefix from local branches tracking origin.

    Looks at branches like "lsyin/foo" that track "origin/lsyin/foo" and picks
    the most common first path component.
    """
    from collections import Counter

    prefixes: list[str] = []
    for b in local_branches:
        if not b.tracking.startswith("origin/"):
            continue
        if "/" in b.name:
            prefixes.append(b.name.split("/", 1)[0])

    if not prefixes:
        return None

    most_common, count = Counter(prefixes).most_common(1)[0]
    return most_common if count >= 2 else None


def classify(
    main: str, remote_prefix: Optional[str] = None
) -> dict[Category, list[Branch]]:
    """Parse `git branch -vv` and classify each branch.

    If remote_prefix is given (or auto-detected), also finds stale remote
    branches on origin matching that prefix with no local counterpart.
    """
    output = _git("branch", "-vv", "--no-color")
    current = _git("rev-parse", "--abbrev-ref", "HEAD")
    merged = _get_merged_into(main)

    result: dict[Category, list[Branch]] = {c: [] for c in Category}
    all_local: list[Branch] = []

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

        branch = Branch(
            name=name,
            commit=commit,
            tracking=tracking,
            status=status,
            message=message.strip(),
            is_worktree=is_wt,
            category=cat,
            is_merged=name in merged,
        )
        result[cat].append(branch)
        all_local.append(branch)

    # --- Remote stale detection ---
    prefix = remote_prefix or _detect_user_prefix(all_local)
    if prefix:
        # Collect names of all local branches that track origin/
        local_tracking_refs = set()
        for b in all_local:
            if b.tracking.startswith("origin/"):
                local_tracking_refs.add(b.tracking.removeprefix("origin/"))
            # Also add branches whose name matches the remote ref
            local_tracking_refs.add(b.name)
        # Also add current branch and main
        local_tracking_refs.add(current)
        local_tracking_refs.add(main)

        remote_output = _git("branch", "-r", "-v", "--no-color")
        for line in remote_output.splitlines():
            rm = _REMOTE_LINE_RE.match(line)
            if not rm:
                continue
            ref_name, commit, message = rm.groups()
            # Skip HEAD, main
            if ref_name in ("HEAD", main, f"HEAD -> origin/{main}"):
                continue
            if " -> " in ref_name:
                continue
            # Only show branches matching the user's prefix
            if not ref_name.startswith(f"{prefix}/"):
                continue
            # Skip if there's already a local branch for this ref
            if ref_name in local_tracking_refs:
                continue

            result[Category.REMOTE_STALE].append(
                Branch(
                    name=ref_name,
                    commit=commit,
                    tracking=f"origin/{ref_name}",
                    status="",
                    message=message.strip(),
                    is_worktree=False,
                    category=Category.REMOTE_STALE,
                    is_remote_only=True,
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
    if b.is_remote_only:
        return cyan_text("origin")
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

_SECTION_ORDER = [
    Category.NOT_MINE,
    Category.MINE_MERGED,
    Category.MINE_ACTIVE,
    Category.REMOTE_STALE,
]
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
                    merged_tag = f"  {green_text('merged')}" if b.is_merged else ""
                    lines.append(
                        f"  {arrow} [{check}] {b.name:<{max_name}}  {tracking}"
                        f"  {dim(b.commit[:9])}{merged_tag}"
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
# Deletion helpers
# ---------------------------------------------------------------------------


def _delete_local(name: str, dry_run: bool) -> bool:
    if dry_run:
        typer.echo(f"  {dim('(dry-run)')} would delete local {name}")
        return True
    r = subprocess.run(["git", "branch", "-D", name], capture_output=True, text=True)
    if r.returncode == 0:
        typer.echo(f"  {green_text('✓')} {name}")
        return True
    typer.echo(f"  {red_text('✗')} {name}: {r.stderr.strip()}")
    return False


def _delete_remote(ref: str, dry_run: bool) -> bool:
    if dry_run:
        typer.echo(f"  {dim('(dry-run)')} would delete origin/{ref}")
        return True
    r = subprocess.run(
        ["git", "push", "origin", "--delete", ref], capture_output=True, text=True
    )
    if r.returncode == 0:
        typer.echo(f"  {green_text('✓')} origin/{ref} {dim('(remote)')}")
        return True
    typer.echo(f"  {red_text('✗')} origin/{ref}: {r.stderr.strip()}")
    return False


# ---------------------------------------------------------------------------
# Worktree pruning
# ---------------------------------------------------------------------------

_WT_PR_RE = re.compile(r"-pr-(\d+)$")


@dataclass
class _StaleWorktree:
    path: Path
    branch: str
    pr_number: str  # "" if no associated PR
    reason: str  # e.g. "MERGED", "CLOSED", "merged into main"
    selected: bool = False


def _list_worktrees() -> list[tuple[Path, str]]:
    """Return (path, branch) for each non-bare worktree from git."""
    out = _git("worktree", "list", "--porcelain")
    worktrees: list[tuple[Path, str]] = []
    path: Optional[Path] = None
    branch = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch "):
            branch = line.removeprefix("branch refs/heads/")
        elif line == "" and path is not None:
            worktrees.append((path, branch))
            path = None
            branch = ""
    if path is not None:
        worktrees.append((path, branch))
    return worktrees


def _find_pr_for_branch(branch: str) -> Optional[tuple[str, str]]:
    """Find the PR number and state for a branch via gh.

    Returns (pr_number, state) or None if no PR found.
    """
    r = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,state",
            "-q",
            '.[0] | "\\(.number) \\(.state)"',
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = r.stdout.strip().split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def _find_stale_worktrees() -> list[_StaleWorktree]:
    """Find worktrees whose associated PR is merged or closed."""
    worktrees = _list_worktrees()
    repo_root = Path(_git("rev-parse", "--show-toplevel"))
    stale: list[_StaleWorktree] = []

    for wt_path, branch in worktrees:
        if wt_path == repo_root:
            continue

        # For *-pr-NNN worktrees, extract PR number directly
        m = _WT_PR_RE.search(wt_path.name)
        if m:
            pr_number = m.group(1)
            r = subprocess.run(
                ["gh", "pr", "view", pr_number, "--json", "state", "-q", ".state"],
                capture_output=True,
                text=True,
            )
            state = r.stdout.strip() if r.returncode == 0 else None
            if state in ("MERGED", "CLOSED"):
                stale.append(
                    _StaleWorktree(
                        path=wt_path,
                        branch=branch,
                        pr_number=pr_number,
                        reason=state,
                    )
                )
            continue

        # For other worktrees, check if branch has an associated PR
        pr_info = _find_pr_for_branch(branch)
        if pr_info:
            pr_number, state = pr_info
            if state in ("MERGED", "CLOSED"):
                stale.append(
                    _StaleWorktree(
                        path=wt_path,
                        branch=branch,
                        pr_number=pr_number,
                        reason=state,
                    )
                )

    return stale


def _remove_worktree(wt: _StaleWorktree, dry_run: bool) -> bool:
    if dry_run:
        typer.echo(f"  {dim('(dry-run)')} would remove {wt.path.name}")
        return True
    r = subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt.path)],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        typer.echo(f"  {green_text('✓')} {wt.path.name}")
        return True
    typer.echo(f"  {red_text('✗')} {wt.path.name}: {r.stderr.strip()}")
    return False


def _reason_display(wt: _StaleWorktree) -> str:
    """Format the reason column for a stale worktree."""
    if wt.pr_number:
        color = green_text if wt.reason == "MERGED" else red_text
        return f"PR #{wt.pr_number} {color(wt.reason)}"
    return yellow_text(wt.reason)


def _prune_worktrees(dry_run: bool) -> None:
    """Find and interactively remove stale worktrees."""
    typer.echo(f"\n{bold('Scanning worktrees...')}")
    stale = _find_stale_worktrees()
    if not stale:
        typer.echo("No stale worktrees found.")
        return

    # Display stale worktrees
    typer.echo(f"\nFound {len(stale)} stale worktree(s):\n")
    for wt in stale:
        typer.echo(f"  {wt.path.name:<40} {_reason_display(wt)}" f"  {dim(wt.branch)}")

    typer.echo("")
    typer.echo(dim("Remove? [a(ll)/N/enter=select] "), nl=False)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    typer.echo(ch)

    if ch.lower() == "a":
        for wt in stale:
            wt.selected = True
    elif ch in ("\r", "\n"):
        # Default: pick individually
        for wt in stale:
            typer.echo(
                f"  {wt.path.name} ({_reason_display(wt)}) " f"{dim('[y/N]')} ",
                nl=False,
            )
            old2 = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                choice = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old2)
            typer.echo(choice)
            wt.selected = choice.lower() == "y"
    else:
        typer.echo("Skipped worktree cleanup.")
        return

    selected = [wt for wt in stale if wt.selected]
    if not selected:
        typer.echo("No worktrees selected.")
        return

    typer.echo(f"\nRemoving {len(selected)} worktree(s):\n")
    removed = 0
    for wt in selected:
        if _remove_worktree(wt, dry_run):
            removed += 1

    # Also run git worktree prune to clean up stale refs
    if not dry_run:
        subprocess.run(["git", "worktree", "prune"], capture_output=True)

    if dry_run:
        typer.echo(
            f"\n{yellow_text('Dry run:')} {len(selected)} worktree(s) would be removed."
        )
    elif removed:
        typer.echo(f"\n{green_text('Done:')} removed {removed} worktree(s).")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def interactive_prune(
    main: Optional[str] = None,
    dry_run: bool = False,
    remote_prefix: Optional[str] = None,
    worktree: bool = True,
) -> None:
    if main is None:
        main = _detect_main_branch()

    grouped = classify(main, remote_prefix=remote_prefix)
    total = sum(len(v) for v in grouped.values())

    if total == 0:
        typer.echo("No branches to prune (only main and current branch remain).")
    else:
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
        else:
            # Separate local and remote-only branches
            local_branches = [b for b in to_delete if not b.is_remote_only]
            remote_only = [b for b in to_delete if b.is_remote_only]

            local_deleted = 0
            remote_deleted = 0

            # Delete local branches
            if local_branches:
                typer.echo(f"\nDeleting {len(local_branches)} local branch(es):\n")
                for b in local_branches:
                    if _delete_local(b.name, dry_run):
                        local_deleted += 1
                    # Also delete remote ref on origin if it exists
                    ref = b.origin_ref
                    if ref and ref != main:
                        if _delete_remote(ref, dry_run):
                            remote_deleted += 1

            # Delete remote-only branches
            if remote_only:
                typer.echo(f"\nDeleting {len(remote_only)} remote branch(es):\n")
                for b in remote_only:
                    if _delete_remote(b.name, dry_run):
                        remote_deleted += 1

            # Summary
            if dry_run:
                total_would = len(local_branches) + len(remote_only)
                typer.echo(
                    f"\n{yellow_text('Dry run:')} {total_would} branch(es) would be deleted."
                )
            else:
                parts = []
                if local_deleted:
                    parts.append(f"{local_deleted} local")
                if remote_deleted:
                    parts.append(f"{remote_deleted} remote")
                if parts:
                    typer.echo(
                        f"\n{green_text('Done:')} deleted {', '.join(parts)} branch(es)."
                    )

    # Worktree cleanup phase
    if worktree:
        _prune_worktrees(dry_run)
