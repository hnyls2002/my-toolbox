"""Tests for gpt_kit.browser — run_js error handling and client parsing."""

import asyncio
import json
import subprocess

import pytest

from my_toolbox.gpt_kit import browser


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["osascript"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --- run_js error mapping --------------------------------------------------


def test_run_js_returns_stdout(monkeypatch):
    monkeypatch.setattr(browser, "_osascript", lambda js, t: _proc(stdout="hello\n"))
    assert browser.run_js("x") == "hello"


def test_run_js_toggle_off(monkeypatch):
    monkeypatch.setattr(
        browser,
        "_osascript",
        lambda js, t: _proc(stdout="JS_ERROR:Executing JavaScript ... is turned off."),
    )
    with pytest.raises(browser.BrowserError, match="Apple Events"):
        browser.run_js("x")


def test_run_js_automation_denied(monkeypatch):
    monkeypatch.setattr(
        browser,
        "_osascript",
        lambda js, t: _proc(returncode=1, stderr="Not authorized (-1743)"),
    )
    with pytest.raises(browser.BrowserError, match="Automation"):
        browser.run_js("x")


def test_run_js_not_macos(monkeypatch):
    def _boom(js, t):
        raise FileNotFoundError()

    monkeypatch.setattr(browser, "_osascript", _boom)
    with pytest.raises(browser.BrowserError, match="macOS"):
        browser.run_js("x")


# --- BrowserClient.list_all ------------------------------------------------


def test_list_all_parses(monkeypatch):
    payload = {
        "items": [
            {"id": "a", "title": "First", "update_time": "2026-01-15T08:30:00Z"},
            {"id": "b", "title": None, "update_time": 1700000000.0},
        ],
        "total": 2,
    }
    monkeypatch.setattr(browser, "run_js", lambda js: json.dumps(payload))
    convs = asyncio.run(browser.BrowserClient().list_all())
    assert [c.id for c in convs] == ["a", "b"]
    assert convs[1].title == ""  # None title normalized


def test_list_all_not_logged_in(monkeypatch):
    monkeypatch.setattr(browser, "run_js", lambda js: json.dumps({"error": 401}))
    with pytest.raises(browser.BrowserError, match="signed in"):
        asyncio.run(browser.BrowserClient().list_all())


# --- BrowserClient.delete_many ---------------------------------------------


def test_delete_many_aggregates(monkeypatch):
    def fake_run_js(js):
        # Every id in this chunk: id0/id2 ok (200), id1 fails (404).
        ids = json.loads(js[js.index("[") : js.index("]") + 1])
        return json.dumps({i: (200 if i != "id1" else 404) for i in ids})

    monkeypatch.setattr(browser, "run_js", fake_run_js)
    ok, errs = asyncio.run(
        browser.BrowserClient().delete_many(["id0", "id1", "id2"], chunk=2)
    )
    assert ok == ["id0", "id2"]
    assert [e[0] for e in errs] == ["id1"]
