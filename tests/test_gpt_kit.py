"""Tests for gpt_kit — time parsing, labels, and the TUI delete flow."""

import asyncio

from my_toolbox.gpt_kit import history as history_mod
from my_toolbox.gpt_kit.browser import Conversation, parse_time

# ---------------------------------------------------------------------------
# parse_time
# ---------------------------------------------------------------------------


def test_parse_time_epoch():
    assert parse_time(1700000000.0) == 1700000000.0
    assert parse_time(1700000000) == 1700000000.0


def test_parse_time_iso():
    assert parse_time("2026-01-15T08:30:00Z") == parse_time("2026-01-15T08:30:00+00:00")


def test_parse_time_none_and_garbage():
    assert parse_time(None) is None
    assert parse_time("not-a-date") is None


# ---------------------------------------------------------------------------
# Conversation.label
# ---------------------------------------------------------------------------


def test_label_untitled():
    assert "(untitled)" in Conversation(id="x", title="", update_time=None).label


def test_label_contains_title():
    assert (
        "Hello" in Conversation(id="x", title="Hello", update_time=1700000000.0).label
    )


# ---------------------------------------------------------------------------
# End-to-end TUI flow (headless, fake browser client)
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.deleted = []
        self._items = [
            Conversation(id=f"id{i}", title=t, update_time=1700000000.0 + i)
            for i, t in enumerate(["alpha chat", "beta notes", "alpha plan", "gamma"])
        ]

    async def list_all(self, progress=None):
        if progress:
            progress(len(self._items), len(self._items))
        return list(self._items)

    async def delete_many(self, ids, progress=None, chunk=25):
        self.deleted.extend(ids)
        if progress:
            progress(len(ids), len(ids))
        return list(ids), []


def test_focus_starts_on_list_slash_enters_search(monkeypatch):
    monkeypatch.setattr(history_mod, "BrowserClient", _FakeClient)

    async def scenario():
        app = history_mod.HistoryApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            # Default focus is the list (command mode), not the search box.
            assert app.focused is not None and app.focused.id == "list"
            # `/` enters the filter box.
            await pilot.press("slash")
            await pilot.pause()
            assert app.focused.id == "search"
            # Enter returns to the list.
            await pilot.press("enter")
            await pilot.pause()
            assert app.focused.id == "list"
            # `/` then Escape also returns to the list.
            await pilot.press("slash")
            await pilot.pause()
            assert app.focused.id == "search"
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused.id == "list"

    asyncio.run(scenario())


def test_vim_jk_navigation(monkeypatch):
    monkeypatch.setattr(history_mod, "BrowserClient", _FakeClient)

    async def scenario():
        app = history_mod.HistoryApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            lst = app.query_one("#list")
            lst.focus()
            lst.highlighted = 0
            await pilot.pause()
            await pilot.press("j")  # vim down
            await pilot.pause()
            assert lst.highlighted == 1
            await pilot.press("j")
            await pilot.pause()
            assert lst.highlighted == 2
            await pilot.press("k")  # vim up
            await pilot.pause()
            assert lst.highlighted == 1

    asyncio.run(scenario())


def test_filter_select_delete_flow(monkeypatch):
    monkeypatch.setattr(history_mod, "BrowserClient", _FakeClient)

    async def scenario():
        app = history_mod.HistoryApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app.conversations) == 4

            # Filter to "alpha" -> 2 shown, then select all shown.
            inp = app.query_one("#search")
            inp.value = "alpha"
            await pilot.pause()
            assert len(app._visible_ids) == 2
            app.action_select_all()
            await pilot.pause()
            assert len(app.selected_ids) == 2

            # Selection survives clearing the filter.
            inp.value = ""
            await pilot.pause()
            assert len(app._visible_ids) == 4
            assert len(app.selected_ids) == 2

            # Delete: confirm, then the deletions run and the list shrinks.
            app.action_delete()
            await pilot.pause()
            await pilot.click("#confirm-yes")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app.conversations) == 2
            assert set(app.client.deleted) == {"id0", "id2"}

    asyncio.run(scenario())
