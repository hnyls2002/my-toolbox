"""Tests for branch_prune — focus on is_merged field and merged tag display."""

from unittest.mock import patch

from my_toolbox.git.branch_prune import (
    Branch,
    Category,
    Selector,
    _tracking_display,
    classify,
)

# ---------------------------------------------------------------------------
# classify: is_merged field
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


@patch("my_toolbox.git.branch_prune._detect_user_prefix", return_value=None)
@patch("my_toolbox.git.branch_prune._git")
def test_classify_is_merged(mock_git, _mock_prefix):
    outputs = _fake_git_outputs()
    mock_git.side_effect = _mock_git_factory(outputs)

    grouped = classify("main")

    all_branches = [b for bs in grouped.values() for b in bs]
    by_name = {b.name: b for b in all_branches}

    # feat/alpha: in merged set → is_merged=True, category=MINE_MERGED
    assert by_name["feat/alpha"].is_merged is True
    assert by_name["feat/alpha"].category == Category.MINE_MERGED

    # feat/beta: gone + in merged set → is_merged=True
    assert by_name["feat/beta"].is_merged is True
    assert by_name["feat/beta"].category == Category.MINE_MERGED

    # feat/gamma: not merged → is_merged=False, category=MINE_ACTIVE
    assert by_name["feat/gamma"].is_merged is False
    assert by_name["feat/gamma"].category == Category.MINE_ACTIVE

    # review/other: not mine → is_merged=False
    assert by_name["review/other"].is_merged is False
    assert by_name["review/other"].category == Category.NOT_MINE


# ---------------------------------------------------------------------------
# Selector: merged tag in rendered output
# ---------------------------------------------------------------------------


def _make_branch(name, is_merged=False, category=Category.MINE_MERGED):
    return Branch(
        name=name,
        commit="abc123def",
        tracking="origin/" + name,
        status="gone" if category == Category.MINE_MERGED else "",
        message="test",
        is_worktree=False,
        category=category,
        is_merged=is_merged,
    )


def test_selector_shows_merged_tag():
    grouped = {
        Category.NOT_MINE: [],
        Category.MINE_MERGED: [
            _make_branch("feat/merged-one", is_merged=True),
            _make_branch("feat/gone-only", is_merged=False),
        ],
        Category.MINE_ACTIVE: [],
        Category.REMOTE_STALE: [],
    }
    sel = Selector(grouped)
    rendered = sel.render(40)

    # The merged branch should show the "merged" tag
    assert "merged" in rendered
    # Verify both branch names appear
    assert "feat/merged-one" in rendered
    assert "feat/gone-only" in rendered


def test_selector_merged_tag_only_on_merged_branches():
    merged_b = _make_branch("feat/yes", is_merged=True)
    not_merged_b = _make_branch("feat/no", is_merged=False)

    grouped = {
        Category.NOT_MINE: [],
        Category.MINE_MERGED: [merged_b, not_merged_b],
        Category.MINE_ACTIVE: [],
        Category.REMOTE_STALE: [],
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
# _tracking_display
# ---------------------------------------------------------------------------


def test_tracking_display_gone():
    b = _make_branch("x", category=Category.MINE_MERGED)
    b.status = "gone"
    b.tracking = "origin/x"
    display = _tracking_display(b)
    assert "gone" in display


def test_tracking_display_local():
    b = _make_branch("x", category=Category.MINE_ACTIVE)
    b.tracking = "(local)"
    b.status = ""
    display = _tracking_display(b)
    assert "local" in display
