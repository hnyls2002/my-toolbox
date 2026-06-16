"""Reach ChatGPT's backend-api through your logged-in Chrome (via AppleScript).

ChatGPT's backend sits behind Cloudflare, which blocks plain HTTP clients with a
managed challenge. The web app reaches it with same-origin requests from a real
browser tab, so we do the same: run JavaScript in a logged-in chatgpt.com tab
via AppleScript. The browser clears Cloudflare, and the access token is read
automatically from the page's own session (/api/auth/session), so there is
nothing to paste.

macOS + Google Chrome only. One-time setup: enable Chrome menu
View -> Developer -> Allow JavaScript from Apple Events.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime

# AppleScript runner: find (or open) a chatgpt.com tab, then run the JS passed
# as the first argument and return its result. JS arrives via argv so we never
# have to escape it into the AppleScript source.
_RUNNER = r"""
on run argv
    set theJS to item 1 of argv
    tell application "Google Chrome"
        set theTab to missing value
        repeat with w in windows
            repeat with t in tabs of w
                if (URL of t) starts with "https://chatgpt.com" then
                    set theTab to t
                    exit repeat
                end if
            end repeat
            if theTab is not missing value then exit repeat
        end repeat
        if theTab is missing value then
            if (count of windows) is 0 then make new window
            set theTab to make new tab at end of tabs of front window with properties {URL:"https://chatgpt.com/"}
            repeat 80 times
                delay 0.25
                try
                    if (execute theTab javascript "document.readyState") is "complete" then exit repeat
                on error e
                    return "JS_ERROR:" & e
                end try
            end repeat
        end if
        try
            return execute theTab javascript theJS
        on error e
            return "JS_ERROR:" & e
        end try
    end tell
end run
"""

# backend-api authenticates with a Bearer token (cookies alone resolve to an
# empty context), so every call first reads the access token from the page's
# own /api/auth/session, then sends it. The request still originates from the
# real browser tab, which is what satisfies Cloudflare.
_TOKEN_JS = (
    "var _s=new XMLHttpRequest();_s.open('GET','/api/auth/session',false);"
    "_s.send();var _tok=JSON.parse(_s.responseText).accessToken;"
)

# Paginate the whole conversation list with same-origin synchronous XHR.
_LIST_JS = (
    r"""
(function () {
  %s
  // The API's "total" field is unreliable, so paginate until a short page.
  var out = [], offset = 0, limit = 100;
  while (true) {
    var x = new XMLHttpRequest();
    x.open('GET', '/backend-api/conversations?offset=' + offset + '&limit=' + limit + '&order=updated', false);
    x.setRequestHeader('Authorization', 'Bearer ' + _tok);
    x.send();
    if (x.status !== 200) {
      return JSON.stringify({error: x.status});
    }
    var d = JSON.parse(x.responseText);
    var items = d.items || [];
    for (var i = 0; i < items.length; i++) {
      out.push({id: items[i].id, title: items[i].title, update_time: items[i].update_time});
    }
    offset += limit;
    if (items.length < limit) break;
  }
  return JSON.stringify({items: out, total: out.length});
})()
"""
    % _TOKEN_JS
)


def _delete_js(ids: list[str]) -> str:
    """JS that PATCHes is_visible=false for each id and returns {id: status}."""
    return (
        "(function(){%svar ids=%s,res={};for(var i=0;i<ids.length;i++){"
        "var x=new XMLHttpRequest();"
        "x.open('PATCH','/backend-api/conversation/'+ids[i],false);"
        "x.setRequestHeader('Content-Type','application/json');"
        "x.setRequestHeader('Authorization','Bearer '+_tok);"
        "try{x.send(JSON.stringify({is_visible:false}));res[ids[i]]=x.status;}"
        "catch(e){res[ids[i]]=-1;}}"
        "return JSON.stringify(res);})()"
    ) % (_TOKEN_JS, json.dumps(ids))


def parse_time(value) -> float | None:
    """ChatGPT returns time as epoch float (detail) or ISO string (list)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Conversation:
    id: str
    title: str
    update_time: float | None

    @property
    def label(self) -> str:
        when = ""
        if self.update_time:
            when = datetime.fromtimestamp(self.update_time).strftime("%Y-%m-%d %H:%M")
        return f"{when:<16}  {self.title or '(untitled)'}"


class BrowserError(Exception):
    """Raised when Chrome isn't ready or the request failed."""


def _osascript(js: str, timeout: float) -> subprocess.CompletedProcess:
    """Run the AppleScript runner with `js` as its argument. Test seam."""
    return subprocess.run(
        ["osascript", "-e", _RUNNER, js],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_js(js: str, timeout: float = 120.0) -> str:
    """Run JS in a chatgpt.com tab and return its string result, or raise."""
    try:
        proc = _osascript(js, timeout)
    except FileNotFoundError as exc:  # no osascript -> not macOS
        raise BrowserError("This requires macOS (osascript not found).") from exc
    except subprocess.TimeoutExpired as exc:
        raise BrowserError("Chrome did not respond in time.") from exc

    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "Not authorized" in err or "-1743" in err:
            raise BrowserError(
                "Chrome automation not permitted. Allow it under System Settings "
                "-> Privacy & Security -> Automation -> your terminal -> Google Chrome."
            )
        raise BrowserError(f"osascript failed: {err or 'unknown error'}")

    out = proc.stdout.rstrip("\n")
    if out.startswith("JS_ERROR:"):
        msg = out[len("JS_ERROR:") :].strip()
        if "turned off" in msg:
            raise BrowserError(
                "Chrome blocks JavaScript from Apple Events. Enable it once: Chrome "
                "menu -> View -> Developer -> Allow JavaScript from Apple Events."
            )
        raise BrowserError(f"Chrome JavaScript error: {msg}")
    return out


def _explain_status(status: int) -> str:
    if status == 401:
        return "Not signed in to ChatGPT in Chrome (open chatgpt.com and log in)."
    if status == 403:
        return "Cloudflare blocked even the in-browser request (unexpected)."
    return f"ChatGPT API returned status {status}."


def check() -> list[str]:
    """Return readiness status lines, or raise BrowserError if JS access is off."""
    run_js("1+1")
    lines = ["Chrome JavaScript access: OK"]
    data = json.loads(run_js(_LIST_JS))
    if "error" in data:
        lines.append("ChatGPT login: " + _explain_status(data["error"]))
    else:
        lines.append(f"ChatGPT login: OK ({len(data.get('items', []))} conversations)")
    return lines


class BrowserClient:
    """Conversation list/delete, executed inside the logged-in Chrome tab."""

    async def list_all(self, progress=None) -> list[Conversation]:
        raw = await asyncio.to_thread(run_js, _LIST_JS)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrowserError(f"Unexpected response from Chrome: {raw[:160]}") from exc
        if "error" in data:
            raise BrowserError(_explain_status(data["error"]))
        convs = [
            Conversation(
                id=it["id"],
                title=it.get("title") or "",
                update_time=parse_time(it.get("update_time")),
            )
            for it in data.get("items", [])
        ]
        if progress:
            progress(len(convs), len(convs))
        return convs

    async def delete_many(
        self, ids: list[str], progress=None, chunk: int = 25
    ) -> tuple[list[str], list[tuple[str, str]]]:
        succeeded: list[str] = []
        errors: list[tuple[str, str]] = []
        total = len(ids)
        done = 0
        for start in range(0, total, chunk):
            part = ids[start : start + chunk]
            raw = await asyncio.to_thread(run_js, _delete_js(part))
            try:
                statuses = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise BrowserError(f"Unexpected delete response: {raw[:160]}") from exc
            for cid in part:
                if statuses.get(cid) == 200:
                    succeeded.append(cid)
                else:
                    errors.append((cid, _explain_status(statuses.get(cid) or 0)))
            done += len(part)
            if progress:
                progress(done, total)
        return succeeded, errors
