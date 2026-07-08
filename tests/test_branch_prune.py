"""Tests for branch_prune — lifecycle grouping (NOT_MINE / DONE / ACTIVE)."""

from unittest.mock import patch

from my_toolbox.git.branch_prune import Branch, Category, Selector, classify

# ---------------------------------------------------------------------------
# classify: lifecycle grouping
# ---------------------------------------------------------------------------


def _fake_git_outputs(main="main"):
    """Return a dict mapping git arg tuples to stdout strings."""
    return {
        ("branch", "-vv", "--no-color"): (
            "  feat/alpha   abc1234 [origin/feat/alpha] add alpha\n"
            "  feat/beta    def5678 [origin/feat/beta: gone] old beta\n"
            "  feat/gamma   111aaaa [origin/feat/gamma] gamma wip\n"
            "  review/other 222bbbb [upstream/review/other] someone else\n"
        ),
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("branch", "--merged", main, "--no-color"): (
            "* main\n  feat/alpha\n  feat/beta\n"
        ),
        ("branch", "-r", "-v", "--no-color"): "",
    }


def _mock_git_factory(outputs: dict):
    def _git(*args):
        return outputs.get(args, "")

    return _git


@patch("my_toolbox.git.branch_prune._check_pr_states_parallel", return_value={})
@patch("my_toolbox.git.branch_prune._git")
def test_classify_lifecycle_groups(mock_git, _mock_pr):
    outputs = _fake_git_outputs()
    mock_git.side_effect = _mock_git_factory(outputs)

    grouped = classify("main")

    all_branches = [b for bs in grouped.values() for b in bs]
    by_name = {b.name: b for b in all_branches}

    # feat/alpha: merged into main -> DONE
    assert by_name["feat/alpha"].is_merged is True
    assert by_name["feat/alpha"].category == Category.DONE

    # feat/beta: gone + merged -> DONE
    assert by_name["feat/beta"].is_merged is True
    assert by_name["feat/beta"].category == Category.DONE

    # feat/gamma: unmerged, no PR -> ACTIVE
    assert by_name["feat/gamma"].is_merged is False
    assert by_name["feat/gamma"].category == Category.ACTIVE

    # review/other: tracks a non-origin remote -> NOT_MINE (ownership dominates)
    assert by_name["review/other"].is_merged is False
    assert by_name["review/other"].category == Category.NOT_MINE


@patch("my_toolbox.git.branch_prune._check_pr_states_parallel")
@patch("my_toolbox.git.branch_prune._git")
def test_closed_pr_branch_goes_to_done(mock_git, mock_pr):
    # A local branch with no gone/merged signal but a CLOSED PR must land in
    # DONE, not linger in ACTIVE. This is the core consistency fix.
    outputs = {
        ("branch", "-vv", "--no-color"): (
            "  feat/closed abc1234 [origin/feat/closed] abandoned\n"
            "  feat/open   def5678 [origin/feat/open] wip\n"
        ),
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("branch", "--merged", "main", "--no-color"): "* main\n",
        ("branch", "-r", "-v", "--no-color"): "",
    }
    mock_git.side_effect = _mock_git_factory(outputs)
    mock_pr.return_value = {"feat/closed": ("7", "CLOSED"), "feat/open": ("8", "OPEN")}

    grouped = classify("main")
    by_name = {b.name: b for bs in grouped.values() for b in bs}

    assert by_name["feat/closed"].category == Category.DONE
    assert by_name["feat/closed"].pr_state == "CLOSED"
    assert by_name["feat/closed"].is_merged is False  # closed != merged
    assert by_name["feat/open"].category == Category.ACTIVE


# ---------------------------------------------------------------------------
# classify: remote-only staleness is decided by PR state, not by prefix match
# ---------------------------------------------------------------------------


@patch("my_toolbox.git.branch_prune._has_push_access", return_value=True)
@patch("my_toolbox.git.branch_prune._check_pr_states_parallel")
@patch("my_toolbox.git.branch_prune._git")
def test_remote_only_staleness_by_pr_state(mock_git, mock_pr, _mock_push):
    # Four remote-only branches under the prefix, none with a local counterpart.
    outputs = {
        ("branch", "-vv", "--no-color"): "",
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("branch", "--merged", "main", "--no-color"): "* main\n",
        ("branch", "-r", "-v", "--no-color"): (
            "  origin/lsyin/done   abc1234 merged work\n"
            "  origin/lsyin/closed 222bbbb closed pr\n"
            "  origin/lsyin/open   def5678 open work\n"
            "  origin/lsyin/nopr   99aa00b wip no pr\n"
            "  origin/main         111aaaa main tip\n"
        ),
    }
    mock_git.side_effect = _mock_git_factory(outputs)
    # done=MERGED, closed=CLOSED, open=OPEN, nopr=absent (no PR at all).
    mock_pr.return_value = {
        "lsyin/done": ("1", "MERGED"),
        "lsyin/closed": ("3", "CLOSED"),
        "lsyin/open": ("2", "OPEN"),
    }

    grouped = classify("main", remote_prefix="lsyin")
    # Remote-only stale branches now fold into DONE, marked is_remote_only.
    done_remote = {b.name: b for b in grouped[Category.DONE] if b.is_remote_only}

    # Only merged/closed PR branches are stale; open and no-PR are excluded.
    assert set(done_remote) == {"lsyin/done", "lsyin/closed"}
    assert done_remote["lsyin/done"].is_merged is True
    assert done_remote["lsyin/closed"].is_merged is False  # closed != merged
    assert done_remote["lsyin/done"].category == Category.DONE


@patch("my_toolbox.git.branch_prune._has_push_access", return_value=True)
@patch("my_toolbox.git.branch_prune._check_pr_states_parallel", return_value={})
@patch("my_toolbox.git.branch_prune._git")
def test_no_prefix_skips_remote_detection(mock_git, _mock_pr, mock_push):
    outputs = {
        ("branch", "-vv", "--no-color"): "",
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("branch", "--merged", "main", "--no-color"): "* main\n",
        ("branch", "-r", "-v", "--no-color"): (
            "  origin/lsyin/done abc1234 merged work\n"
        ),
    }
    mock_git.side_effect = _mock_git_factory(outputs)

    # No remote_prefix -> remote detection is skipped entirely (no inference).
    grouped = classify("main")

    assert [b for b in grouped[Category.DONE] if b.is_remote_only] == []
    mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# Selector: merged tag in rendered output
# ---------------------------------------------------------------------------


def _make_branch(name, is_merged=False, category=Category.DONE):
    return Branch(
        name=name,
        commit="abc123def",
        tracking="origin/" + name,
        status="gone" if category == Category.DONE else "",
        message="test",
        is_worktree=False,
        category=category,
        is_merged=is_merged,
    )


def test_selector_merged_tag_only_on_merged_branches():
    merged_b = _make_branch("feat/yes", is_merged=True)
    not_merged_b = _make_branch("feat/no", is_merged=False)

    grouped = {
        Category.NOT_MINE: [],
        Category.DONE: [merged_b, not_merged_b],
        Category.ACTIVE: [],
    }
    sel = Selector(grouped)
    lines = sel._render_all_lines()

    merged_lines = [l for l in lines if "feat/yes" in l]
    not_merged_lines = [l for l in lines if "feat/no" in l]

    # "merged" text appears on the merged branch line
    assert any("merged" in l for l in merged_lines)
    # "merged" text does NOT appear on the non-merged branch line
    assert not any("merged" in l for l in not_merged_lines)


# ---------------------------------------------------------------------------
# Selector: adaptive width / no wrapping
# ---------------------------------------------------------------------------

import re as _re


def _vis(s):
    return _re.sub(r"\x1b\[[0-9;]*m", "", s)


def _many_active(n):
    return {
        Category.ACTIVE: [
            Branch(
                name=f"lsyin/some-long-branch-name-{i:02d}",
                commit="a1b2c3d4e",
                tracking="origin/x",
                status="",
                message="m",
                is_worktree=False,
                category=Category.ACTIVE,
                pr_number=str(100 + i),
                pr_state="OPEN",
                edit_date="2d ago",
            )
            for i in range(n)
        ]
    }


def test_render_lines_never_exceed_terminal_width():
    # The core fix: every emitted line fits the terminal so nothing wraps
    # (wrapping would desync the scroll/cursor math).
    sel = Selector(_many_active(6))
    for width in (120, 100, 80, 60, 45):
        for line in sel._render_all_lines(width):
            assert len(_vis(line)) <= width, (width, _vis(line))


def test_adaptive_drops_commit_before_narrowing_name():
    sel = Selector(_many_active(3))
    wide = _vis("".join(sel._render_all_lines(120)))
    narrow = _vis("".join(sel._render_all_lines(80)))
    # Wide shows every column; narrow drops Commit first but keeps Name + PR.
    assert "Commit" in wide
    assert "Commit" not in narrow
    assert "Name" in narrow and "PR" in narrow


def test_cursor_line_tracked_by_render_pass():
    # _line_starts is populated by the same pass that renders, so there is no
    # second hard-coded layout to drift out of sync.
    sel = Selector(_many_active(4))
    sel._render_all_lines(100)
    assert set(sel._line_starts) == set(range(len(sel.items)))
    assert sel._line_starts[sel.cursor] >= 0
