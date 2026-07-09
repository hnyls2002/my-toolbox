"""Interactive branch pruning — classify and bulk-delete local + remote branches.

Three groups on a single lifecycle axis:
1. Not Mine - tracks a non-origin remote (fetched for PR review)
2. Done     - finished: upstream gone, merged into main, or PR merged/closed
3. Active   - mine, unmerged, still in progress

Location (local vs remote-only) and the exact reason (merged/closed/gone) are
row-level details, not separate groups. With --remote-prefix, my remote-only
branches with a merged/closed PR are folded into Done (shown as 'origin' in the
Tracking column).

Usage:
    rgit prune                  # interactive mode (local branches only)
    rgit prune --dry-run        # preview without deleting
    rgit prune --main master    # use 'master' as base branch
    rgit prune --remote-prefix myuser  # also detect stale origin/myuser* branches
"""

import json
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
    # A single lifecycle axis. Location (local vs remote-only) and the exact
    # reason (merged/closed/gone) are per-row details, not separate groups.
    NOT_MINE = "Not Mine"  # tracks a non-origin remote (fetched for review)
    DONE = "Done (merged/closed/gone)"  # finished -- safe to prune
    ACTIVE = "Active"  # mine, unmerged, still in progress


@dataclass
class Branch:
    name: str
    commit: str
    tracking: str  # "origin/foo", "user/foo", "(local)"
    status: str  # "gone", "ahead 3, behind 2", ""
    message: str
    is_worktree: bool
    category: Category
    is_merged: bool = False  # git ancestor of main (git branch --merged; -d ok)
    is_remote_only: bool = False  # True for remote-only branches
    pr_state: str = ""  # "MERGED", "CLOSED", "OPEN" from gh
    pr_number: str = ""  # PR number (without '#'), "" if no PR
    edit_date: str = ""  # last-commit date, compact relative, e.g. "2d ago"
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
    # rstrip only: `git branch -vv` relies on leading whitespace per line
    # (first line has "  " indent for non-current branches); strip() would
    # eat the first line's indent and break _LINE_RE.
    return r.stdout.rstrip()


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


_REL_DATE_RE = re.compile(r"^(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago$")
# "month" -> "mo" so it doesn't collide with "minute" -> "m"
_REL_UNIT = {
    "second": "s",
    "minute": "m",
    "hour": "h",
    "day": "d",
    "week": "w",
    "month": "mo",
    "year": "y",
}


def _compact_date(rel: str) -> str:
    """Compact git's verbose relative date, e.g. '2 days ago' -> '2d ago'."""
    m = _REL_DATE_RE.match(rel)
    if not m:
        return rel  # "just now", "in the future", etc. -- leave as-is
    n, unit = m.groups()
    return f"{n}{_REL_UNIT[unit]} ago"


def _get_edit_dates() -> dict[str, str]:
    """Return {refname:short -> compact relative date} for local + origin refs.

    One `for-each-ref` call covers every local branch and every origin/* ref,
    so both regular and remote-only branches get a last-edit date.
    """
    out = _git(
        "for-each-ref",
        "--format=%(refname:short)%09%(committerdate:relative)",
        "refs/heads/",
        "refs/remotes/origin/",
    )
    dates: dict[str, str] = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        ref, rel = line.split("\t", 1)
        dates[ref] = _compact_date(rel)
    return dates


# ---------------------------------------------------------------------------
# PR state lookup (one batched gh call)
# ---------------------------------------------------------------------------

_PR_LIST_LIMIT = 400  # most-recent PRs fetched in the single batch call


def _fetch_all_prs() -> dict[str, tuple[str, str]]:
    """Return {headRefName: (number, state)} from ONE `gh pr list` call.

    One batch call replaces the previous one-gh-call-per-branch fan-out. Only
    the most-recent _PR_LIST_LIMIT PRs are fetched; hitting that cap is logged
    (never silently dropped), since older branches would then show no PR.
    """
    r = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--limit",
            str(_PR_LIST_LIMIT),
            "--json",
            "headRefName,number,state",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    try:
        prs = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    if len(prs) >= _PR_LIST_LIMIT:
        sys.stderr.write(
            f"  Note: only the {_PR_LIST_LIMIT} most-recent PRs were checked; "
            "older branches may show no PR.\n"
        )
    # gh lists newest first, so the first PR seen for a head branch is the most
    # recent one -- that is the state worth showing.
    result: dict[str, tuple[str, str]] = {}
    for pr in prs:
        head = pr.get("headRefName")
        if head and head not in result:
            result[head] = (str(pr["number"]), pr["state"])
    return result


def _has_push_access() -> bool:
    """Check if the user has push access to the origin remote via gh."""
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "viewerPermission", "-q", ".viewerPermission"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False
    return r.stdout.strip() in ("ADMIN", "MAINTAIN", "WRITE")


def classify(
    main: str, remote_prefix: Optional[str] = None
) -> dict[Category, list[Branch]]:
    """Parse `git branch -vv` and group branches on one lifecycle axis.

    Groups: NOT_MINE (ownership) / DONE (finished) / ACTIVE (in progress).
    Ownership dominates -- a branch tracking a non-origin remote is always
    NOT_MINE. Otherwise DONE fires on any 'finished' signal (upstream gone,
    merged into main, or PR merged/closed), else ACTIVE. PR state is fetched
    once and drives DONE uniformly, so a closed-PR branch never lingers in
    ACTIVE.

    If remote_prefix is given, also finds stale remote branches on origin:
    those matching that prefix, with no local counterpart, AND whose PR is
    merged/closed. The prefix only scopes the candidate set; staleness is
    decided purely by PR state (the two are orthogonal).
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

        # Provisional group (ownership dominates). DONE may also be set later
        # from PR state; ACTIVE is the fallback until then.
        if tracking != "(local)" and not tracking.startswith("origin/"):
            cat = Category.NOT_MINE
        elif "gone" in status or name in merged:
            cat = Category.DONE
        else:
            cat = Category.ACTIVE

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

    # --- Remote-only candidate collection (prefix-scoped) ---
    # The prefix is always explicit (--remote-prefix); it is never inferred.
    # It only scopes WHICH origin branches are candidates -- whether each is
    # stale is decided separately below, purely by PR state.
    prefix = remote_prefix
    can_push = _has_push_access() if prefix else False
    remote_candidates: list[Branch] = []
    if prefix and can_push:
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
            # Symrefs like "origin/HEAD -> origin/main" have no commit hash, so
            # _REMOTE_LINE_RE never matches them; only real branches reach here.
            if ref_name == main:
                continue
            # Only consider branches matching the user's prefix (strict string
            # prefix, no "/" anchor -- so "lsyin" also catches "lsyin-xxx/...").
            if not ref_name.startswith(prefix):
                continue
            # Skip if there's already a local branch for this ref
            if ref_name in local_tracking_refs:
                continue

            remote_candidates.append(
                Branch(
                    name=ref_name,
                    commit=commit,
                    tracking=f"origin/{ref_name}",
                    status="",
                    message=message.strip(),
                    is_worktree=False,
                    category=Category.DONE,  # provisional; kept only if merged/closed
                    is_remote_only=True,
                )
            )

    # --- Fetch PR state (one uniform signal for local + remote branches) ---
    # Catches squash merges on local branches and decides staleness for
    # remote-only candidates alike: a branch is "done" iff its PR is MERGED,
    # and remote-only candidates are kept only when MERGED/CLOSED.
    branches_to_check = [
        b
        for cat in (Category.NOT_MINE, Category.ACTIVE, Category.DONE)
        for b in result[cat]
        if not b.is_worktree
    ] + remote_candidates
    if branches_to_check:
        all_prs = _fetch_all_prs()
        for b in branches_to_check:
            info = all_prs.get(b.name)
            if info:
                b.pr_number, b.pr_state = info
        # NOTE: do NOT set is_merged from a MERGED PR. is_merged means "git
        # ancestor of main" (git branch --merged), which is what `git branch -d`
        # accepts. A squash/rebase-merged PR is NOT an ancestor, so it must be
        # force-deleted (-D); safety for it is handled by _is_safe_delete's
        # separate pr_state check, not by is_merged.

    # Remote-only branches are prunable only when their PR is merged/closed.
    # Open / no-PR remote branches are not stale, so they are not listed.
    for b in remote_candidates:
        if b.pr_state in ("MERGED", "CLOSED"):
            result[Category.DONE].append(b)

    # PR state drives DONE uniformly: an ACTIVE branch whose PR turned out
    # merged OR closed moves to DONE (a closed PR never lingers in ACTIVE).
    still_active = []
    for b in result[Category.ACTIVE]:
        if b.pr_state in ("MERGED", "CLOSED"):
            b.category = Category.DONE
            result[Category.DONE].append(b)
        else:
            still_active.append(b)
    result[Category.ACTIVE] = still_active

    # Attach last-edit dates (one for-each-ref call for all refs)
    dates = _get_edit_dates()
    for b in all_local:
        b.edit_date = dates.get(b.name, "")
    for b in remote_candidates:
        b.edit_date = dates.get(f"origin/{b.name}", "")

    return result


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


_ANSI_RESET = "\033[0m"
_BG_CURSOR = "\033[48;5;238m"  # medium gray background for cursor row
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi_len(s: str) -> int:
    """Return visible length of a string (excluding ANSI escape sequences)."""
    return len(_ANSI_RE.sub("", s))


def _bg_line(line: str, width: int, bg: str) -> str:
    """Apply a background color to a full-width line, preserving inner colors."""
    inner = line.replace(_ANSI_RESET, f"{_ANSI_RESET}{bg}")
    pad = max(0, width - _strip_ansi_len(inner))
    return f"{bg}{inner}{' ' * pad}{_ANSI_RESET}"


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


def _pad_visible(s: str, width: int) -> str:
    """Right-pad a possibly-ANSI-colored string to a visible width."""
    return s + " " * max(0, width - _strip_ansi_len(s))


def _clip_visible(s: str, width: int) -> str:
    """Truncate to `width` visible columns, keeping ANSI codes intact.

    A trailing reset is appended when anything is cut, so color never bleeds
    past the clip. Guarantees the result never wraps a `width`-column terminal.
    """
    if _strip_ansi_len(s) <= width:
        return s
    out: list[str] = []
    vis = 0
    i = 0
    while i < len(s) and vis < width:
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(s[i])
        vis += 1
        i += 1
    out.append(_ANSI_RESET)
    return "".join(out)


def _fit(text: str, width: int) -> str:
    """Ellipsize plain (un-colored) text to at most `width` columns."""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _pr_display(b: Branch) -> str:
    """Format the PR column: '#12345 OPEN/MERGED/CLOSED', colored by state."""
    if b.pr_number:
        num = dim(f"#{b.pr_number}")
        color = {
            "MERGED": green_text,
            "CLOSED": red_text,
            "OPEN": cyan_text,
        }.get(b.pr_state, lambda x: x)
        state = color(b.pr_state) if b.pr_state else ""
        return f"{num} {state}".rstrip()
    if b.is_merged:
        # Merged into main with no discoverable PR (e.g. plain merge)
        return green_text("merged")
    return ""


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


# A section is keyed either by a branch Category or by this worktree sentinel.
_WORKTREE_SECTION = "worktrees"


@dataclass
class _Header:
    section: object  # Category | str (_WORKTREE_SECTION)


@dataclass
class _ToggleAll:
    section: object  # Category | str (_WORKTREE_SECTION)


@dataclass
class _BranchRow:
    branch: Branch


@dataclass
class _WorktreeRow:
    worktree: "_StaleWorktree"


@dataclass
class _Spacer:
    pass


_Item = _Header | _ToggleAll | _BranchRow | _WorktreeRow | _Spacer

_SECTION_ORDER = [
    Category.NOT_MINE,
    Category.DONE,
    Category.ACTIVE,
]
_FOOTER_LINES = 3  # blank + status + blank

# Worktree section columns (path name / status / branch).
_WT_NAME_CAP = 40

# Adaptive branch-table columns. Name flexes to fill the leftover terminal
# width; the optional columns are dropped right-to-left (Commit, then Date,
# then Tracking) when space runs low. Name and PR are never dropped.
_PREFIX_W = 8  # visible width of the "  > [x] " row prefix
_DATE_W = 8
_TRACK_W = 12
_COMMIT_W = 9
_PR_CAP = 16  # max width reserved for the PR column
_NAME_MIN = 8  # absolute minimum Name width (all optionals dropped)
_NAME_CAP = 36  # Name width honored in full before ellipsizing kicks in


# ---------------------------------------------------------------------------
# Interactive selector
# ---------------------------------------------------------------------------


class Selector:
    def __init__(
        self,
        grouped: dict[Category, list[Branch]],
        worktrees: Optional[list["_StaleWorktree"]] = None,
    ):
        self.grouped = grouped
        self.worktrees = worktrees or []
        self.items: list[_Item] = []
        self.cursor = 0
        self.scroll = 0
        # item index -> first physical line, recorded during _render_all_lines
        # so cursor/scroll math reads the SAME layout that was rendered.
        self._line_starts: dict[int, int] = {}
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
        if self.worktrees:
            self.items.append(_Header(_WORKTREE_SECTION))
            self.items.append(_ToggleAll(_WORKTREE_SECTION))
            for wt in self.worktrees:
                self.items.append(_WorktreeRow(wt))
            self.items.append(_Spacer())

    # -- navigation ---------------------------------------------------------

    def _is_selectable(self, idx: int) -> bool:
        if idx < 0 or idx >= len(self.items):
            return False
        item = self.items[idx]
        if isinstance(item, (_ToggleAll, _WorktreeRow)):
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

    def _section_rows(self, section) -> list:
        """Selectable payloads (Branch or _StaleWorktree) in a section."""
        if section == _WORKTREE_SECTION:
            return list(self.worktrees)
        return self._category_branches(section)

    def _cursor_section(self):
        """Return the section key (Category | sentinel) the cursor is in."""
        item = self.items[self.cursor]
        if isinstance(item, _ToggleAll):
            return item.section
        if isinstance(item, _BranchRow):
            return item.branch.category
        if isinstance(item, _WorktreeRow):
            return _WORKTREE_SECTION
        return None

    # -- key handling -------------------------------------------------------

    def handle_key(self, key: str) -> Optional[str]:
        if key in ("up", "k"):
            self._move(-1)
        elif key in ("down", "j"):
            self._move(1)
        elif key in ("space", "o"):
            item = self.items[self.cursor]
            if isinstance(item, _BranchRow) and not item.branch.is_worktree:
                item.branch.selected = not item.branch.selected
            elif isinstance(item, _WorktreeRow):
                item.worktree.selected = not item.worktree.selected
            elif isinstance(item, _ToggleAll):
                self._toggle_section(item.section)
        elif key in ("a", "A"):
            section = self._cursor_section()
            if section is not None:
                self._toggle_section(section)
        elif key == "enter":
            return "confirm"
        elif key in ("q", "quit"):
            return "cancel"
        return None

    def _toggle_section(self, section):
        rows = self._section_rows(section)
        if not rows:
            return
        all_sel = all(r.selected for r in rows)
        for r in rows:
            r.selected = not all_sel

    # -- rendering ----------------------------------------------------------

    def render(self, term_height: int, term_width: int = 80) -> str:
        """Return a full screen's worth of rendered text.

        Every emitted line is clipped to `term_width` (no wrapping), so one
        list line == one physical row and the scroll math below stays exact.
        """
        all_lines = self._render_all_lines(term_width)
        cursor_line = self._line_starts.get(self.cursor, 0)

        total_sel = len(self.selected_branches()) + len(self.selected_worktrees())
        footer = dim(
            f"  j/k Navigate  o/Space Toggle  a Toggle section"
            f"  Enter Delete ({total_sel})  q Cancel"
        )

        visible = max(term_height - _FOOTER_LINES, 5)
        total = len(all_lines)

        if total <= visible:
            self.scroll = 0
            return "\n".join(all_lines) + "\n\n" + footer

        # Scrollable: reserve two rows for the up/down indicators so they never
        # overwrite a real line. Content height is fixed, keeping cursor/scroll
        # math exact no matter how many groups are shown.
        content_h = max(1, visible - 2)
        if cursor_line < self.scroll:
            self.scroll = cursor_line
        elif cursor_line >= self.scroll + content_h:
            self.scroll = cursor_line - content_h + 1
        self.scroll = max(0, min(self.scroll, total - content_h))

        top_more = self.scroll > 0
        bot_more = self.scroll + content_h < total
        window = [dim("  ↑ more") if top_more else ""]
        window += all_lines[self.scroll : self.scroll + content_h]
        window.append(dim("  ↓ more") if bot_more else "")
        return "\n".join(window) + "\n\n" + footer

    def _render_all_lines(self, term_width: int = 80) -> list[str]:
        lines: list[str] = []
        self._line_starts = {}
        plan = self._plan_columns(term_width)
        wt_plan = self._worktree_plan()
        name_w = plan["name"]

        for i, item in enumerate(self.items):
            self._line_starts[i] = len(lines)
            is_cur = i == self.cursor

            if isinstance(item, _Header):
                section = item.section
                rows = self._section_rows(section)
                total = len(rows)
                sel = sum(1 for r in rows if r.selected)
                if section == _WORKTREE_SECTION:
                    title, suffix = "Worktrees (stale)", ""
                else:
                    title = section.value
                    wt = sum(
                        1
                        for it in self.items
                        if isinstance(it, _BranchRow)
                        and it.branch.category == section
                        and it.branch.is_worktree
                    )
                    suffix = f" +{wt} worktree" if wt else ""
                lines.append("")
                lines.append(
                    _clip_visible(
                        f"  {bold(f'━━ {title} ({sel}/{total}){suffix} ━━━━━━━━━━')}",
                        term_width,
                    )
                )

            elif isinstance(item, _ToggleAll):
                arrow = cyan_text("›") if is_cur else " "
                rows = self._section_rows(item.section)
                all_sel = all(r.selected for r in rows) if rows else False
                check = green_text("✓") if all_sel else " "
                line = f"  {arrow} [{check}] {cyan_text('Select all / Deselect all')}"
                line = _clip_visible(line, term_width)
                if is_cur:
                    line = _bg_line(line, term_width, _BG_CURSOR)
                lines.append(line)
                # Column header labels (dim, no background)
                labels = (
                    self._worktree_header_labels(wt_plan)
                    if item.section == _WORKTREE_SECTION
                    else self._header_labels(plan)
                )
                lines.append(_clip_visible(dim(labels), term_width))

            elif isinstance(item, _BranchRow):
                b = item.branch
                if b.is_worktree:
                    lines.append(
                        _clip_visible(
                            f"    {dim(f'[w] {_fit(b.name, name_w)}  (worktree — skip)')}",
                            term_width,
                        )
                    )
                else:
                    arrow = cyan_text("›") if is_cur else " "
                    check = green_text("✓") if b.selected else " "
                    line = f"  {arrow} [{check}] {self._row_body(b, plan)}"
                    line = _clip_visible(line, term_width)
                    if is_cur:
                        line = _bg_line(line, term_width, _BG_CURSOR)
                    lines.append(line)

            elif isinstance(item, _WorktreeRow):
                wt = item.worktree
                arrow = cyan_text("›") if is_cur else " "
                check = green_text("✓") if wt.selected else " "
                line = f"  {arrow} [{check}] {self._worktree_row_body(wt, wt_plan)}"
                line = _clip_visible(line, term_width)
                if is_cur:
                    line = _bg_line(line, term_width, _BG_CURSOR)
                lines.append(line)

            elif isinstance(item, _Spacer):
                lines.append("")

        return lines

    def _plan_columns(self, term_width: int) -> dict:
        """Decide Name width and which optional columns fit `term_width`.

        Optional columns drop right-to-left (Commit, Date, Tracking) until the
        full branch name fits; Name and PR are never dropped. Name is honored up
        to _NAME_CAP (so one very long branch name can't nuke every column) and
        ellipsized only when even Name + PR alone overflow. The plan is computed
        once per render so every row shares the same column widths.
        """
        rows = [it.branch for it in self.items if isinstance(it, _BranchRow)]
        max_name = max((len(b.name) for b in rows), default=_NAME_MIN)
        target_name = min(_NAME_CAP, max_name)
        pr_widths = [_strip_ansi_len(_pr_display(b)) for b in rows]
        pr_w = min(_PR_CAP, max(pr_widths)) if pr_widths else 0

        show = {"date": True, "tracking": True, "commit": True}

        def fixed_cost() -> int:
            cost = _PREFIX_W
            if show["date"]:
                cost += 2 + _DATE_W
            if show["tracking"]:
                cost += 2 + _TRACK_W
            if show["commit"]:
                cost += 2 + _COMMIT_W
            if pr_w:
                cost += 2 + pr_w
            return cost

        # Drop lowest-priority columns first until the full name fits.
        for col in ("commit", "date", "tracking"):
            if term_width - fixed_cost() >= target_name:
                break
            show[col] = False

        name_w = max(_NAME_MIN, min(term_width - fixed_cost(), target_name))
        return {"name": name_w, "pr": pr_w, **show}

    def _header_labels(self, plan: dict) -> str:
        cols = [f"{'Name':<{plan['name']}}"]
        if plan["date"]:
            cols.append(f"{'Date':<{_DATE_W}}")
        if plan["tracking"]:
            cols.append(f"{'Tracking':<{_TRACK_W}}")
        if plan["commit"]:
            cols.append(f"{'Commit':<{_COMMIT_W}}")
        if plan["pr"]:
            cols.append("PR")
        return "        " + "  ".join(cols)

    def _row_body(self, b: Branch, plan: dict) -> str:
        name_w = plan["name"]
        cols = [f"{_fit(b.name, name_w):<{name_w}}"]
        if plan["date"]:
            cols.append(dim(f"{b.edit_date:<{_DATE_W}}"))
        if plan["tracking"]:
            track = _clip_visible(_tracking_display(b), _TRACK_W)
            cols.append(_pad_visible(track, _TRACK_W))
        if plan["commit"]:
            cols.append(dim(f"{b.commit[:_COMMIT_W]:<{_COMMIT_W}}"))
        if plan["pr"]:
            cols.append(_pr_display(b))
        return "  ".join(cols)

    def _worktree_plan(self) -> dict:
        """Column widths for the worktree section (path name / status)."""
        name_w = min(
            _WT_NAME_CAP,
            max((len(wt.path.name) for wt in self.worktrees), default=8),
        )
        reason_w = max(
            (_strip_ansi_len(_reason_display(wt)) for wt in self.worktrees),
            default=0,
        )
        return {"name": name_w, "reason": reason_w}

    def _worktree_header_labels(self, plan: dict) -> str:
        return (
            f"        {'Worktree':<{plan['name']}}  "
            f"{'Status':<{plan['reason']}}  Branch"
        )

    def _worktree_row_body(self, wt: "_StaleWorktree", plan: dict) -> str:
        name = f"{_fit(wt.path.name, plan['name']):<{plan['name']}}"
        reason = _pad_visible(_reason_display(wt), plan["reason"])
        return f"{name}  {reason}  {dim(wt.branch)}"

    def selected_branches(self) -> list[Branch]:
        return [
            it.branch
            for it in self.items
            if isinstance(it, _BranchRow) and it.branch.selected
        ]

    def selected_worktrees(self) -> list["_StaleWorktree"]:
        return [
            it.worktree
            for it in self.items
            if isinstance(it, _WorktreeRow) and it.worktree.selected
        ]


# ---------------------------------------------------------------------------
# Deletion helpers
# ---------------------------------------------------------------------------


def _delete_local(name: str, dry_run: bool, force: bool) -> bool:
    # -d lets git refuse an unexpectedly-unmerged branch as a last safety net;
    # -D is only used where we already know deletion is intended (unmerged
    # selections and squash-merged branches that -d would wrongly reject).
    flag = "-D" if force else "-d"
    if dry_run:
        verb = "force-delete" if force else "delete"
        typer.echo(f"  {dim('(dry-run)')} would {verb} local {name}")
        return True
    r = subprocess.run(["git", "branch", flag, name], capture_output=True, text=True)
    if r.returncode == 0:
        typer.echo(f"  {green_text('✓')} {name}")
        return True
    typer.echo(f"  {red_text('✗')} {name}: {r.stderr.strip()}")
    return False


def _delete_tracking_ref(ref: str, dry_run: bool) -> None:
    """Delete the local tracking ref (refs/remotes/origin/xxx), not the remote."""
    if dry_run:
        typer.echo(f"  {dim('(dry-run)')} would remove tracking ref origin/{ref}")
        return
    r = subprocess.run(
        ["git", "branch", "-d", "-r", f"origin/{ref}"],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        typer.echo(f"  {dim(f'  removed tracking ref origin/{ref}')}")


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
    if "does not exist" in r.stderr:
        typer.echo(f"  {dim(f'  origin/{ref} (already gone)')}")
        return False
    typer.echo(f"  {red_text('✗')} origin/{ref}: {r.stderr.strip()}")
    return False


def _is_safe_delete(b: Branch) -> bool:
    """A delete is safe when the work already lives on main.

    True for an ancestor merge (`is_merged`) or a merged PR (squash/rebase, so
    not an ancestor but still landed). Everything else -- closed PR, gone but
    unmerged, or an explicitly-selected active branch -- may drop local-only
    commits and is treated as a force-delete.
    """
    return b.is_merged or b.pr_state == "MERGED"


def _delete_note(b: Branch) -> str:
    """One-line reason shown in the force-delete confirmation."""
    if b.pr_state == "CLOSED":
        return f"PR #{b.pr_number} closed, not merged"
    if "gone" in b.status:
        return "upstream gone, not merged"
    return "unmerged"


def _confirm_force_deletes(risky: list[Branch], safe_count: int) -> bool:
    """List the unmerged branches about to be force-deleted, then confirm."""
    typer.echo(
        f"\n{yellow_text('Force-deleting UNMERGED branch(es) - local commits may be lost:')}"
    )
    for b in risky:
        loc = dim(" (remote)") if b.is_remote_only else ""
        typer.echo(f"  {red_text(b.name)}{loc}  {dim(_delete_note(b))}")
    if safe_count:
        typer.echo(dim(f"  (+{safe_count} safe branch(es) already on main)"))
    return typer.confirm("Proceed with force-delete?", default=False)


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


def _remove_worktrees(selected: list[_StaleWorktree], dry_run: bool) -> int:
    """Remove the given stale worktrees; return how many were removed."""
    removed = 0
    for wt in selected:
        if _remove_worktree(wt, dry_run):
            removed += 1
    if not dry_run and removed:
        # Clean up any now-dangling worktree admin entries.
        subprocess.run(["git", "worktree", "prune"], capture_output=True)
    return removed


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

    # Refresh remote-tracking refs so deleted remote branches surface as 'gone'
    # and stale origin/* refs disappear before classification.
    sys.stderr.write("Fetching origin...")
    sys.stderr.flush()
    subprocess.run(
        ["git", "fetch", "--prune", "origin"], capture_output=True, text=True
    )
    sys.stderr.write("\r" + " " * 20 + "\r")
    sys.stderr.flush()

    if remote_prefix is None:
        typer.echo(dim("Remote stale detection off (pass --remote-prefix to enable)."))

    grouped = classify(main, remote_prefix=remote_prefix)

    if worktree:
        sys.stderr.write("Scanning worktrees...")
        sys.stderr.flush()
        worktrees = _find_stale_worktrees()
        sys.stderr.write("\r" + " " * 24 + "\r")
        sys.stderr.flush()
    else:
        worktrees = []

    if sum(len(v) for v in grouped.values()) + len(worktrees) == 0:
        typer.echo("Nothing to prune (no stale branches or worktrees).")
        return

    selector = Selector(grouped, worktrees)

    # Enter alternate screen buffer, hide cursor
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()

    action = "cancel"
    try:
        while True:
            term_size = os.get_terminal_size()
            screen = selector.render(term_size.lines, term_size.columns)

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

    branches = selector.selected_branches()
    wts = selector.selected_worktrees()
    if not branches and not wts:
        typer.echo("Nothing selected.")
        return

    local_branches = [b for b in branches if not b.is_remote_only]
    remote_only = [b for b in branches if b.is_remote_only]

    # Confirm before force-deleting any unmerged branch (live runs only; dry-run
    # just reports). Safe (already-on-main) deletes and worktrees need no gate.
    risky = [b for b in branches if not _is_safe_delete(b)]
    if risky and not dry_run:
        if not _confirm_force_deletes(risky, len(branches) - len(risky)):
            typer.echo("Cancelled.")
            return

    local_deleted = remote_deleted = 0

    # Delete local branches + their tracking refs. Merged branches use a plain
    # `-d`; unmerged/squash-merged ones need `-D`.
    if local_branches:
        typer.echo(f"\nDeleting {len(local_branches)} local branch(es):\n")
        for b in local_branches:
            if _delete_local(b.name, dry_run, force=not b.is_merged):
                local_deleted += 1
            ref = b.origin_ref
            if ref:
                _delete_tracking_ref(ref, dry_run)

    if remote_only:
        typer.echo(f"\nDeleting {len(remote_only)} remote branch(es):\n")
        for b in remote_only:
            if _delete_remote(b.name, dry_run):
                remote_deleted += 1

    wt_removed = 0
    if wts:
        typer.echo(f"\nRemoving {len(wts)} worktree(s):\n")
        wt_removed = _remove_worktrees(wts, dry_run)

    # Summary
    if dry_run:
        n = len(local_branches) + len(remote_only) + len(wts)
        typer.echo(f"\n{yellow_text('Dry run:')} {n} item(s) would be removed.")
    else:
        parts = []
        if local_deleted:
            parts.append(f"{local_deleted} local")
        if remote_deleted:
            parts.append(f"{remote_deleted} remote")
        if wt_removed:
            parts.append(f"{wt_removed} worktree")
        if parts:
            typer.echo(f"\n{green_text('Done:')} removed {', '.join(parts)}.")
