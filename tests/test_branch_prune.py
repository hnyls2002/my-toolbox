"""Tests for branch_prune — lifecycle grouping (NOT_MINE / DONE / ACTIVE)."""

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

from my_toolbox.git.branch_prune import (
    _WORKTREE_SECTION,
    Branch,
    Category,
    Selector,
    _BranchRow,
    _clip_visible,
    _dirty_worktrees,
    _fetch_prs,
    _find_detached_worktrees,
    _is_safe_delete,
    _lock_worktree_branches,
    _origin_repo,
    _pr_display,
    _remove_worktrees,
    _StaleWorktree,
    _strip_ansi_len,
    _WorktreeRow,
    classify,
)

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


@patch("my_toolbox.git.branch_prune._fetch_prs", return_value=({}, {}))
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


@patch("my_toolbox.git.branch_prune._fetch_prs")
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
    mock_pr.return_value = (
        {"feat/closed": ("7", "CLOSED"), "feat/open": ("8", "OPEN")},
        {},
    )

    grouped = classify("main")
    by_name = {b.name: b for bs in grouped.values() for b in bs}

    assert by_name["feat/closed"].category == Category.DONE
    assert by_name["feat/closed"].pr_state == "CLOSED"
    assert by_name["feat/closed"].is_merged is False  # closed != merged
    assert by_name["feat/open"].category == Category.ACTIVE


@patch("my_toolbox.git.branch_prune._fetch_prs")
@patch("my_toolbox.git.branch_prune._git")
def test_squash_merged_branch_is_not_git_ancestor(mock_git, mock_pr):
    # A squash/rebase-merged PR branch: PR is MERGED but the branch is NOT a git
    # ancestor of main (git branch --merged does not list it). is_merged must
    # stay False so deletion uses -D; -d would refuse with "not fully merged".
    # Regression for that exact bug.
    outputs = {
        ("branch", "-vv", "--no-color"): (
            "  lsyin/squash abc1234 [origin/lsyin/squash] squashed feature\n"
        ),
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("branch", "--merged", "main", "--no-color"): "* main\n",  # NOT an ancestor
        ("branch", "-r", "-v", "--no-color"): "",
    }
    mock_git.side_effect = _mock_git_factory(outputs)
    mock_pr.return_value = ({"lsyin/squash": ("42", "MERGED")}, {})

    b = classify("main")[Category.DONE][0]
    assert b.name == "lsyin/squash"
    assert b.pr_state == "MERGED"
    assert b.is_merged is False  # -> force=not is_merged=True -> git branch -D
    assert _is_safe_delete(b) is True  # still safe: work landed on main


# ---------------------------------------------------------------------------
# classify: remote-only staleness is decided by PR state, not by prefix match
# ---------------------------------------------------------------------------


@patch("my_toolbox.git.branch_prune._has_push_access", return_value=True)
@patch("my_toolbox.git.branch_prune._fetch_prs")
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
    mock_pr.return_value = (
        {
            "lsyin/done": ("1", "MERGED"),
            "lsyin/closed": ("3", "CLOSED"),
            "lsyin/open": ("2", "OPEN"),
        },
        {},
    )

    grouped = classify("main", remote_prefix="lsyin")
    # Remote-only stale branches now fold into DONE, marked is_remote_only.
    done_remote = {b.name: b for b in grouped[Category.DONE] if b.is_remote_only}

    # Only merged/closed PR branches are stale; open and no-PR are excluded.
    assert set(done_remote) == {"lsyin/done", "lsyin/closed"}
    assert done_remote["lsyin/done"].pr_state == "MERGED"
    assert done_remote["lsyin/closed"].pr_state == "CLOSED"
    # Remote-only branches are never git ancestors of local main.
    assert done_remote["lsyin/done"].is_merged is False
    assert done_remote["lsyin/done"].category == Category.DONE


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


def test_adaptive_drops_commit_before_narrowing_name():
    sel = Selector(_many_active(3))
    wide = _vis("".join(sel._render_all_lines(120)))
    narrow = _vis("".join(sel._render_all_lines(80)))
    # Wide shows every column; narrow drops Commit first but keeps Name + PR.
    assert "Commit" in wide
    assert "Commit" not in narrow
    assert "Name" in narrow and "PR" in narrow


# ---------------------------------------------------------------------------
# Delete safety tiers
# ---------------------------------------------------------------------------


def _del_branch(is_merged=False, pr_state="", status=""):
    return Branch(
        name="x",
        commit="c",
        tracking="origin/x",
        status=status,
        message="m",
        is_worktree=False,
        category=Category.DONE,
        is_merged=is_merged,
        pr_state=pr_state,
    )


def test_is_safe_delete_tiers():
    # Safe = work is on main (ancestor merge or a merged PR).
    assert _is_safe_delete(_del_branch(is_merged=True)) is True
    assert _is_safe_delete(_del_branch(pr_state="MERGED")) is True
    # Risky = force-delete may lose local commits.
    assert _is_safe_delete(_del_branch(pr_state="CLOSED")) is False
    assert _is_safe_delete(_del_branch(status="gone")) is False
    assert _is_safe_delete(_del_branch()) is False


# ---------------------------------------------------------------------------
# Unified TUI: worktree section
# ---------------------------------------------------------------------------


def _wts(n):
    return [
        _StaleWorktree(
            path=Path(f"/x/toolbox-pr-{i}"),
            branch=f"lsyin/b{i}",
            pr_number=str(i),
            reason="MERGED",
        )
        for i in range(n)
    ]


def test_worktree_section_select_independent():
    sel = Selector(_many_active(2), worktrees=_wts(2))
    # Worktrees appear as their own selectable rows in the same Selector.
    assert any(isinstance(it, _WorktreeRow) for it in sel.items)
    sel._toggle_section(_WORKTREE_SECTION)
    assert len(sel.selected_worktrees()) == 2
    # Toggling the worktree section leaves branch selection untouched. The two
    # sets never overlap: this section holds only worktrees with no branch row.
    assert sel.selected_branches() == []


def test_origin_repo_parses_every_remote_url_form():
    forms = {
        "git@github.com:sgl-project/sglang.git": "sgl-project/sglang",
        "https://github.com/sgl-project/sglang.git": "sgl-project/sglang",
        "https://github.com/sgl-project/sglang": "sgl-project/sglang",
        "ssh://git@github.com/sgl-project/sglang.git": "sgl-project/sglang",
        "git@github.com:hnyls2002/my-toolbox": "hnyls2002/my-toolbox",
    }
    for url, want in forms.items():
        _origin_repo.cache_clear()
        with patch("my_toolbox.git.branch_prune._git", return_value=url):
            assert _origin_repo() == want, url
    _origin_repo.cache_clear()


def test_origin_repo_reads_origin_not_ghs_default():
    # Regression: `gh repo view` resolves gh's default repo, which on a checkout
    # carrying many fork remotes can silently be someone else's fork -- every PR
    # lookup would then miss with no error.
    _origin_repo.cache_clear()
    with patch("my_toolbox.git.branch_prune._git") as g:
        g.return_value = "git@github.com:sgl-project/sglang.git"
        _origin_repo()
    assert g.call_args[0] == ("remote", "get-url", "origin")
    _origin_repo.cache_clear()


def test_dirty_worktrees_probes_in_parallel():
    paths = [Path(f"/wt-{i}") for i in range(4)]
    with patch(
        "my_toolbox.git.branch_prune._worktree_is_dirty",
        side_effect=lambda p: p.name in ("wt-1", "wt-3"),
    ):
        assert _dirty_worktrees(paths) == {Path("/wt-1"), Path("/wt-3")}


def test_dirty_worktree_is_flagged_in_the_row():
    clean = _wt_branch("feat-clean", worktree="/wt-clean")
    messy = _wt_branch("feat-messy", worktree="/wt-messy")
    messy.worktree_dirty = True
    sel = Selector({Category.DONE: [clean, messy]})
    plan = sel._plan_columns(120)

    assert "w!" in _vis_all(sel._row_body(messy, plan))
    # A clean worktree keeps the plain marker -- no false alarm.
    clean_body = _vis_all(sel._row_body(clean, plan))
    assert "w" in clean_body and "w!" not in clean_body


def test_no_worktree_keeps_the_specific_lock_reason():
    # Regression: the --no-worktree pass overwrote "main worktree" with the
    # vaguer "worktree", discarding what classify had already worked out.
    main_wt = _wt_branch("trunk-work", worktree="/repo", locked="main worktree")
    plain_wt = _wt_branch("feat-b", worktree="/wt-b")
    no_wt = _wt_branch("feat-c")

    _lock_worktree_branches({Category.DONE: [main_wt, plain_wt, no_wt]})

    assert main_wt.worktree_locked == "main worktree"  # not clobbered
    assert plain_wt.worktree_locked == "worktree"
    assert no_wt.deletable is True  # no worktree -> untouched


# ---------------------------------------------------------------------------
# OSC 8 hyperlinks on the PR column
# ---------------------------------------------------------------------------


def _linked_branch():
    b = _make_branch("feat/linked", category=Category.DONE)
    b.pr_number, b.pr_state = "12345", "MERGED"
    return b


@contextlib.contextmanager
def _linking(repo="sgl-project/sglang", tty=True):
    """Context controlling whether PR cells render as hyperlinks.

    tty=False keeps the repo resolvable so the ONLY thing suppressing the link
    is the tty gate -- that is what makes the off-tty assertion meaningful.
    """
    with patch("my_toolbox.git.branch_prune._origin_repo", return_value=repo), patch(
        "sys.stdout.isatty", return_value=tty
    ):
        yield


def _vis_all(s):
    """Strip BOTH SGR colors and OSC 8 links -- an independent implementation, so
    a bug in the production regex cannot make these assertions pass."""
    s = _re.sub(r"\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)", "", s)
    return _re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_pr_cell_is_a_hyperlink_to_the_pr():
    with _linking():
        cell = _pr_display(_linked_branch())

    assert "\033]8;;https://github.com/sgl-project/sglang/pull/12345\033\\" in cell
    assert cell.endswith("\033]8;;\033\\")  # link closed
    # The escape must not count as visible width, or every column would shift.
    assert _strip_ansi_len(cell) == len("#12345 MERGED")


def test_clip_does_not_litter_lines_without_links():
    # Regression: the terminator was appended unconditionally, so every clipped
    # line -- section headers included -- carried 6 stray escape bytes.
    clipped = _clip_visible("\033[1m" + "x" * 100 + "\033[0m", 20)
    assert "\033]8" not in clipped


def test_clip_closes_a_hyperlink_it_cuts_through():
    with _linking():
        cell = _pr_display(_linked_branch())
    clipped = _clip_visible(cell, 4)
    # _vis_all, not _strip_ansi_len: the latter shares _ANSI_RE with the code
    # under test, so a broken regex would make both agree and this pass.
    assert len(_vis_all(clipped)) == 4
    # Without the terminator the rest of the screen would stay clickable.
    assert clipped.endswith("\033]8;;\033\\" + "\033[0m")


# ---------------------------------------------------------------------------
# PR lookup: batched GraphQL by head ref, with -pr-<N> directory fallback
# ---------------------------------------------------------------------------


def _graphql_reply(payload: dict, errors=None):
    body = {"data": {"repository": payload}}
    if errors:
        body["errors"] = errors

    class R:
        returncode = 0
        stdout = json.dumps(body)
        stderr = ""

    return R()


@patch("my_toolbox.git.branch_prune._origin_repo", return_value="sgl-project/sglang")
@patch("my_toolbox.git.branch_prune.subprocess.run")
def test_fetch_prs_resolves_refs_and_numbers_in_one_request(run, _repo):
    run.return_value = _graphql_reply(
        {
            "a0": {"nodes": [{"number": 25894, "state": "OPEN"}]},
            "a1": {"nodes": []},  # branch with no PR at all
            "a2": {"number": 28067, "state": "MERGED"},
        }
    )

    by_ref, by_number = _fetch_prs(["lsyin/old", "lsyin/nopr"], ["28067"])

    # A PR thousands of PRs old still resolves -- no recency window.
    assert by_ref == {"lsyin/old": ("25894", "OPEN")}
    assert by_number == {"28067": "MERGED"}
    assert run.call_count == 1  # refs and numbers share the request


@patch("my_toolbox.git.branch_prune._origin_repo", return_value="o/n")
@patch("my_toolbox.git.branch_prune.subprocess.run")
def test_fetch_prs_survives_not_found_errors(run, _repo):
    # An unknown number returns a NOT_FOUND error alongside usable data; the
    # resolved aliases must still be read.
    run.return_value = _graphql_reply(
        {"a0": {"nodes": [{"number": 7, "state": "MERGED"}]}, "a1": None},
        errors=[{"type": "NOT_FOUND", "path": ["repository", "a1"]}],
    )

    by_ref, by_number = _fetch_prs(["feat/x"], ["99999999"])

    assert by_ref == {"feat/x": ("7", "MERGED")}
    assert by_number == {}


@patch("my_toolbox.git.branch_prune._origin_repo", return_value="o/n")
@patch("my_toolbox.git.branch_prune.subprocess.run")
def test_fetch_prs_batches_above_alias_cap(run, _repo):
    run.return_value = _graphql_reply({})
    _fetch_prs([f"b{i}" for i in range(120)])
    # 120 lookups at 50 per request -> 3 requests.
    assert run.call_count == 3


def test_fetch_prs_skips_network_when_nothing_to_look_up():
    with patch("my_toolbox.git.branch_prune.subprocess.run") as run:
        assert _fetch_prs([], []) == ({}, {})
    run.assert_not_called()


@patch("my_toolbox.git.branch_prune._git")
def test_classify_falls_back_to_pr_number_from_worktree_dir(mock_git):
    # A local review branch renamed away from the PR's head ref: no head-ref
    # lookup can find it, but the worktree directory carries the PR number.
    outputs = _wt_git_outputs(
        {
            ("worktree", "list", "--porcelain"): (
                "worktree /repo\nHEAD aaa\nbranch refs/heads/main\n"
                "\n"
                "worktree /sglang-pr-28067\nHEAD bbb\n"
                "branch refs/heads/lsyin/pr-28067-review\n"
            ),
            ("rev-parse", "--show-toplevel"): "/repo",
            ("branch", "-vv", "--no-color"): (
                "+ lsyin/pr-28067-review bbb2222 reviewing\n"
            ),
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("branch", "--merged", "main", "--no-color"): "* main\n",
            ("branch", "-r", "-v", "--no-color"): "",
        }
    )
    mock_git.side_effect = _mock_git_factory(outputs)

    with patch(
        "my_toolbox.git.branch_prune._fetch_prs",
        return_value=({}, {"28067": "MERGED"}),
    ) as fetch:
        grouped = classify("main")

    # The number scraped from the directory name is passed to the lookup...
    assert fetch.call_args[0][1] == ["28067"]
    # ...and rescues a branch that matches no PR head ref anywhere.
    b = grouped[Category.DONE][0]
    assert b.name == "lsyin/pr-28067-review"
    assert (b.pr_number, b.pr_state) == ("28067", "MERGED")


# ---------------------------------------------------------------------------
# autoSetupMerge trap: upstream on origin/main must not be treated as our own
# ---------------------------------------------------------------------------


def _upstream_main_branch(status="ahead 3, behind 1707"):
    # What `git worktree add -b X origin/main` leaves behind: upstream is the
    # start-point, not X's own remote branch.
    return Branch(
        name="lsyin/wip",
        commit="abc1234",
        tracking="origin/main",
        status=status,
        message="m",
        is_worktree=False,
        category=Category.ACTIVE,
    )


def test_origin_ref_never_returns_a_foreign_ref():
    # Regression: origin_ref used to return "main" here, so deleting the branch
    # ran `git branch -d -r origin/main` and nuked the shared tracking ref.
    assert _upstream_main_branch().origin_ref is None
    # A same-named upstream is still this branch's own remote branch.
    own = _make_branch("feat/mine", category=Category.ACTIVE)
    own.tracking = "origin/feat/mine"
    own.status = ""
    assert own.origin_ref == "feat/mine"


def test_gone_on_a_foreign_upstream_is_not_a_done_signal():
    # Once origin/main is missing, every branch with that upstream reports
    # 'gone'. That describes origin/main, not the branch -- so it must not fold
    # unmerged local work into DONE.
    b = _upstream_main_branch(status="gone")
    assert b.own_upstream_gone is False
    # A same-named upstream that vanished IS a real signal.
    mine = _make_branch("feat/mine", category=Category.ACTIVE)
    mine.tracking = "origin/feat/mine"
    mine.status = "gone"
    assert mine.own_upstream_gone is True


@patch("my_toolbox.git.branch_prune._fetch_prs", return_value=({}, {}))
@patch("my_toolbox.git.branch_prune._git")
def test_classify_keeps_foreign_gone_branch_active(mock_git, _mock_pr):
    outputs = {
        ("branch", "-vv", "--no-color"): (
            "  lsyin/trap abc1234 [origin/main: gone] unpushed work\n"
            "  lsyin/real def5678 [origin/lsyin/real: gone] landed\n"
        ),
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("branch", "--merged", "main", "--no-color"): "* main\n",
        ("branch", "-r", "-v", "--no-color"): "",
    }
    mock_git.side_effect = _mock_git_factory(outputs)

    by_name = {b.name: b for bs in classify("main").values() for b in bs}

    # origin/main went missing -> says nothing about lsyin/trap.
    assert by_name["lsyin/trap"].category == Category.ACTIVE
    assert by_name["lsyin/trap"].origin_ref is None
    # Its own upstream went missing -> genuinely done.
    assert by_name["lsyin/real"].category == Category.DONE


def test_foreign_upstream_is_visible_in_tracking_column():
    sel = Selector({Category.ACTIVE: [_upstream_main_branch()]})
    text = _vis("\n".join(sel._render_all_lines(120)))
    # Naming the ref exposes the mis-set upstream instead of showing a bare
    # 'origin ahead 3, behind 1707' that reads as the branch's own state.
    assert "->main" in text


# ---------------------------------------------------------------------------
# Worktree enumeration
# ---------------------------------------------------------------------------

_WT_PORCELAIN = (
    "worktree /repo\n"
    "HEAD aaa111\n"
    "branch refs/heads/trunk-work\n"
    "\n"
    "worktree /wt-a\n"
    "HEAD bbb222\n"
    "branch refs/heads/feat-a\n"
    "\n"
    "worktree /wt-b\n"
    "HEAD ccc333\n"
    "branch refs/heads/feat-b\n"
    "\n"
    "worktree /wt-det-pr-77\n"
    "HEAD ddd444\n"
    "detached\n"
)


def _wt_git_outputs(extra=None):
    outputs = {
        ("worktree", "list", "--porcelain"): _WT_PORCELAIN,
        ("rev-parse", "--show-toplevel"): "/wt-a",
    }
    outputs.update(extra or {})
    return outputs


@patch("my_toolbox.git.branch_prune._git")
def test_find_detached_worktrees_only_branchless(mock_git):
    mock_git.side_effect = _mock_git_factory(_wt_git_outputs())

    found = _find_detached_worktrees({"77": "MERGED"})

    # Branch-bearing worktrees are represented by their branch row instead.
    assert [w.path.name for w in found] == ["wt-det-pr-77"]
    assert found[0].pr_number == "77"
    assert found[0].reason == "MERGED"
    assert found[0].selected is False  # never pre-selected


@patch("my_toolbox.git.branch_prune._git")
def test_detached_worktree_unknown_pr_falls_back(mock_git):
    mock_git.side_effect = _mock_git_factory(_wt_git_outputs())

    # PR 77 is older than the batch window, so its state is unknown.
    found = _find_detached_worktrees({})

    assert found[0].reason == "detached"
    assert found[0].pr_number == ""  # no number rendered without a state


# ---------------------------------------------------------------------------
# classify: worktree attachment
# ---------------------------------------------------------------------------


@patch("my_toolbox.git.branch_prune._fetch_prs", return_value=({}, {}))
@patch("my_toolbox.git.branch_prune._git")
def test_classify_attaches_worktree_and_locks_primary(mock_git, _mock_pr):
    # Seen from /wt-a: feat-a is current (excluded), feat-b sits in a removable
    # worktree, trunk-work sits in the primary one, feat-c has no worktree.
    outputs = _wt_git_outputs(
        {
            ("branch", "-vv", "--no-color"): (
                "* feat-a     aaa1111 init\n"
                "+ feat-b     bbb2222 (/wt-b) init\n"
                "  feat-c     ccc3333 init\n"
                "+ trunk-work ddd4444 (/repo) init\n"
            ),
            ("rev-parse", "--abbrev-ref", "HEAD"): "feat-a",
            ("branch", "--merged", "main", "--no-color"): (
                "  feat-b\n  feat-c\n  trunk-work\n"
            ),
            ("branch", "-r", "-v", "--no-color"): "",
        }
    )
    mock_git.side_effect = _mock_git_factory(outputs)

    by_name = {b.name: b for bs in classify("main").values() for b in bs}

    # Removable linked worktree -> recorded so deletion can remove it first.
    assert by_name["feat-b"].worktree_path == Path("/wt-b")
    assert by_name["feat-b"].deletable is True
    # Primary worktree -> locked, so the branch is not offered for deletion.
    assert by_name["trunk-work"].worktree_locked == "main worktree"
    assert by_name["trunk-work"].deletable is False
    # No worktree at all -> unchanged behaviour.
    assert by_name["feat-c"].worktree_path is None
    assert by_name["feat-c"].deletable is True
    # The current branch is never listed.
    assert "feat-a" not in by_name


def _wt_branch(name, worktree=None, locked=""):
    return Branch(
        name=name,
        commit="abc123def",
        tracking="origin/" + name,
        status="",
        message="m",
        is_worktree=bool(worktree),
        category=Category.DONE,
        is_merged=True,
        worktree_path=Path(worktree) if worktree else None,
        worktree_locked=locked,
    )


def test_worktree_backed_branch_is_selectable():
    plain = _wt_branch("feat-plain")
    with_wt = _wt_branch("feat-wt", worktree="/wt-b")
    locked = _wt_branch("trunk-work", worktree="/repo", locked="main worktree")
    sel = Selector({Category.DONE: [plain, with_wt, locked]})

    sel._toggle_section(Category.DONE)

    # The worktree-backed branch takes part; the locked one is excluded.
    assert {b.name for b in sel.selected_branches()} == {"feat-plain", "feat-wt"}
    assert locked.selected is False

    # And the cursor can never land on the locked row.
    reachable = set()
    for _ in range(len(sel.items) * 2):
        reachable.add(sel.cursor)
        sel._move(1)
    locked_idx = next(
        i
        for i, it in enumerate(sel.items)
        if isinstance(it, _BranchRow) and it.branch is locked
    )
    assert locked_idx not in reachable


def test_locked_row_shows_reason_and_counts_as_kept():
    locked = _wt_branch("trunk-work", worktree="/repo", locked="main worktree")
    sel = Selector({Category.DONE: [_wt_branch("feat-plain"), locked]})
    text = _vis("\n".join(sel._render_all_lines(120)))

    assert "(main worktree — keep)" in text
    assert "+1 kept" in text


def test_worktree_gutter_costs_nothing_without_worktrees():
    # The 'w' gutter is only budgeted when some row actually carries a worktree,
    # so plain runs keep their previous column layout.
    no_wt = Selector({Category.DONE: [_wt_branch("feat-plain")]})
    with_wt = Selector({Category.DONE: [_wt_branch("feat-wt", worktree="/wt-b")]})

    assert no_wt._plan_columns(120)["wt"] == 0
    assert with_wt._plan_columns(120)["wt"] > 0
    assert "w" in _vis(
        with_wt._row_body(with_wt.items[2].branch, with_wt._plan_columns(120))
    )


def test_render_width_holds_across_every_row_kind():
    """No emitted line may exceed the terminal width, or the scroll math desyncs.

    One case over ALL row kinds at once -- worktree gutter, dirty marker, locked
    row, PR column, detached section -- with links both off and on. Five separate
    per-dimension cases used to cover slices of this and never their combination.
    """
    branches = []
    for i in range(4):
        b = _wt_branch(f"lsyin/some-long-branch-name-{i:02d}", worktree=f"/wt-{i}")
        b.worktree_dirty = i % 2 == 0
        b.pr_number, b.pr_state, b.edit_date = f"3350{i}", "MERGED", "2d ago"
        branches.append(b)
    branches.append(_wt_branch("trunk-work", worktree="/repo", locked="main worktree"))
    branches.append(_wt_branch("feat-no-worktree"))
    sel = Selector({Category.DONE: branches}, worktrees=_wts(2))

    for linked in (False, True):
        with _linking(tty=linked):
            for width in (120, 100, 80, 60, 45):
                lines = sel._render_all_lines(width)
                for line in lines:
                    assert len(_vis_all(line)) <= width, (linked, width, _vis_all(line))
                # Off a tty the link escape must not be emitted at all -- the
                # repo is resolvable here, so the tty gate is the only thing
                # that can suppress it.
                if not linked:
                    assert not any("\033]8" in line for line in lines)


# ---------------------------------------------------------------------------
# Removal: failed worktrees must not strand a guaranteed-failing branch delete
# ---------------------------------------------------------------------------


def test_remove_worktrees_reports_failures():
    def fake_run(cmd, **kwargs):
        class R:
            pass

        r = R()
        r.stdout = ""
        # /wt-bad refuses removal; everything else succeeds.
        failed = "/wt-bad" in cmd
        r.returncode = 1 if failed else 0
        r.stderr = "cannot remove" if failed else ""
        return r

    with patch("my_toolbox.git.branch_prune.subprocess.run", side_effect=fake_run):
        removed, failed = _remove_worktrees(
            [Path("/wt-ok"), Path("/wt-bad")], dry_run=False
        )

    assert removed == 1
    assert failed == {Path("/wt-bad")}


def test_remove_worktrees_dry_run_touches_nothing():
    with patch("my_toolbox.git.branch_prune.subprocess.run") as run:
        removed, failed = _remove_worktrees([Path("/wt-a")], dry_run=True)

    assert (removed, failed) == (1, set())
    run.assert_not_called()
