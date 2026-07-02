"""Unit tests for ScrollWindow (ui.py).

Focus: the pure-logic parts -- ``write()`` newline handling (the \\r/\\r\\n
distinction that bit us on real PTY output), on-demand height growth, and the
height cap on scrolling. These need no terminal: they assert on the window's
line buffer, not on rendered ANSI. ``_render`` itself (cursor escapes) is
exercised only via the manual real-host smoke check.

The behavior under test was introduced while wiring streaming output through a
dim scrolling window (#36) and hardened after several real-host bugs:
- \\r\\n (ssh/PTY ONLCR) must be a NEWLINE, not a reset -- else exec renders blank.
- a lone \\r (pip progress) resets the current line (in-place redraw).
- a \\r split from its \\n across write() chunks must still be a newline.
- the window grows on demand (short output -> compact) and caps at `height`.
"""

from my_toolbox.ui import ScrollWindow

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _committed(win: ScrollWindow) -> list[str]:
    """All logically-committed lines: the buffered ones plus the current
    (in-progress) line if it has content -- i.e. everything that's been seen."""
    return win._lines + ([win._cur] if win._cur else [])


def _feed(win: ScrollWindow, *chunks: str) -> list[str]:
    """Write each chunk separately (simulating real chunked reads) and return
    the final set of committed lines."""
    for c in chunks:
        win.write(c)
    return _committed(win)


def _window(**kw) -> ScrollWindow:
    """A window whose render is a no-op (we never __enter__ it, so _render's
    cursor writes go to stdout but don't affect the buffer state we assert on)."""
    return ScrollWindow(**kw)


# ---------------------------------------------------------------------------
# newline handling: \r\n vs lone \r
# ---------------------------------------------------------------------------


def test_crlf_is_newline_not_reset():
    """\\r\\n (ssh/PTY ONLCR output) must commit the line and advance -- the \\r
    is line-ending noise, NOT a redraw. This is the case that blanked exec."""
    win = _window(height=8)
    lines = _feed(win, "hello\r\nworld\r\n")
    assert lines == ["hello", "world"]


def test_plain_lf_newline():
    """Plain \\n (PIPE path, no PTY) -- the original install/pip case."""
    win = _window(height=8)
    lines = _feed(win, "hello\nworld\n")
    assert lines == ["hello", "world"]


def test_lone_cr_resets_current_line():
    """A lone \\r (no \\n after, as pip/docker-pull emit for in-place progress)
    resets the current line's content -- the redraw-in-place semantics."""
    win = _window(height=8)
    lines = _feed(win, "step 1\rstep 2\rstep DONE\n")
    assert lines == ["step DONE"]


def test_cr_redraw_then_newline():
    """Progress redraws (\\r) followed by a terminating \\n commit the final
    content, discarding the intermediate redraws."""
    win = _window(height=8)
    lines = _feed(win, "downloading: 10%\rdownloading: 99%\rdownloading: 100%\n")
    assert lines == ["downloading: 100%"]


def test_no_trailing_newline_keeps_current_line():
    """Output without a final \\n leaves the last content as the in-progress
    line (still visible, not yet committed)."""
    win = _window(height=8)
    win.write("partial\nin-progress")
    assert _committed(win) == ["partial", "in-progress"]


# ---------------------------------------------------------------------------
# \r\n split across chunks
# ---------------------------------------------------------------------------


def test_crlf_split_across_chunks():
    """A \\r that ends one chunk and a \\n that starts the next must still be
    treated as a newline, not a stray reset (the _pending_cr holdback)."""
    win = _window(height=8)
    lines = _feed(win, "hello\r", "\nworld\r", "\n")
    assert lines == ["hello", "world"]


def test_trailing_cr_with_no_more_chunks_is_dropped():
    """A trailing \\r with no following chunk is held (_pending_cr) and never
    resolved -- it doesn't spuriously reset, and it doesn't appear as a line."""
    win = _window(height=8)
    win.write("line1\r")
    # No further write: the pending \r is unresolved; line1 stays committed.
    assert _committed(win) == ["line1"]
    assert win._pending_cr is True


def test_pending_cr_resolved_by_next_chunk_without_lf():
    """If the held \\r is followed by a non-\\n chunk, it's a genuine lone \\r
    (redraw), so the current line resets."""
    win = _window(height=8)
    win.write("abc")  # current line "abc", no newline yet
    win.write("\r")  # trailing \r -> pending
    win.write("xyz")  # resolves the \r as a lone redraw: cur was reset
    assert _committed(win) == ["xyz"]


# ---------------------------------------------------------------------------
# on-demand height growth + height cap (scrolling)
# ---------------------------------------------------------------------------


def test_zero_output_reserves_nothing():
    """No writes -> no reserved rows, no committed lines."""
    win = _window(height=8, desc="empty")
    assert _committed(win) == []
    assert win._reserved == 0


def test_short_output_grows_compactly():
    """A few lines are all visible (below the height cap) -- the buffer-level
    view, independent of the on-demand _render growth."""
    win = _window(height=8)
    _feed(win, "a\nb\nc\n")
    assert win.visible_lines() == ["a", "b", "c"]


def test_more_lines_than_height_scroll():
    """Beyond `height` lines, only the last `height` are 'visible' (older lines
    have scrolled off the top). Asserts on visible_lines() -- the buffer-level
    computation _render draws from, independent of cursor/ANSI state."""
    win = _window(height=4)
    for i in range(10):
        win.write(f"L{i}\n")
    assert win.visible_lines() == ["L6", "L7", "L8", "L9"]


def test_visible_caps_at_height():
    """visible_lines() never returns more than `height` lines, no matter how
    much was written."""
    win = _window(height=5)
    for i in range(12):
        win.write(f"L{i}\n")
    assert win.visible_lines() == ["L7", "L8", "L9", "L10", "L11"]


def test_visible_grows_under_height():
    """Before reaching the cap, visible_lines() tracks the content count."""
    win = _window(height=10)
    win.write("one\ntwo\n")
    assert win.visible_lines() == ["one", "two"]
    win.write("three\n")
    assert win.visible_lines() == ["one", "two", "three"]


# ---------------------------------------------------------------------------
# mixed real-world-ish streams
# ---------------------------------------------------------------------------


def test_mixed_lines_progress_and_newlines():
    """Interleaved committed lines, \\r progress redraws, and \\r\\n newlines --
    the realistic pip/docker-pull shape."""
    win = _window(height=8)
    lines = _feed(win, "line1\r\n", "prog: 10%\rprog: 50%\rprog: 100%\r\n", "line2\r\n")
    assert lines == ["line1", "prog: 100%", "line2"]


def test_empty_chunks_are_noops():
    """Empty write() calls must not perturb state."""
    win = _window(height=8)
    win.write("")
    win.write("")
    assert _committed(win) == []
    assert win._pending_cr is False
