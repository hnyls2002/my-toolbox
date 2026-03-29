"""Tests for extract_claude_chat — JSONL → Markdown extraction."""

import json
import tempfile
from pathlib import Path

from my_toolbox.utils.extract_claude_chat import (
    _clean,
    _extract_text,
    _get_role,
    _should_skip,
    extract,
)

# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


def test_extract_text_plain_string():
    obj = {"message": {"content": "hello world", "role": "user"}}
    assert _extract_text(obj) == "hello world"


def test_extract_text_content_blocks():
    obj = {
        "message": {
            "content": [
                {"type": "text", "text": "part one"},
                {"type": "tool_use", "name": "Read"},
                {"type": "text", "text": "part two"},
            ],
            "role": "assistant",
        }
    }
    assert _extract_text(obj) == "part one\npart two"


def test_extract_text_top_level_content():
    obj = {"role": "user", "content": "top level"}
    assert _extract_text(obj) == "top level"


def test_extract_text_empty():
    assert _extract_text({}) == ""


# ---------------------------------------------------------------------------
# _get_role
# ---------------------------------------------------------------------------


def test_get_role_nested():
    assert _get_role({"message": {"role": "assistant"}}) == "assistant"


def test_get_role_top_level():
    assert _get_role({"role": "user"}) == "user"


# ---------------------------------------------------------------------------
# _clean
# ---------------------------------------------------------------------------


def test_clean_removes_system_reminder():
    text = "before <system-reminder>secret stuff</system-reminder> after"
    assert _clean(text) == "before  after"


def test_clean_strips():
    assert _clean("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------


def test_should_skip_empty():
    assert _should_skip("") is True


def test_should_skip_no_response():
    assert _should_skip("No response requested.") is True


def test_should_skip_command_tags():
    assert _should_skip("<local-command-caveat>stuff") is True
    assert _should_skip("<command-name>foo") is True


def test_should_skip_continuation():
    assert (
        _should_skip(
            "This session is being continued from a previous conversation blah"
        )
        is True
    )


def test_should_not_skip_normal():
    assert _should_skip("Please help me with this code") is False


# ---------------------------------------------------------------------------
# extract (end-to-end)
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]):
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_extract_simple_qa():
    records = [
        {"role": "user", "content": "What is 1+1?"},
        {"message": {"role": "assistant", "content": "2"}, "type": ""},
    ]
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
        p = Path(f.name)
    _write_jsonl(p, records)

    md = extract(p)
    assert "### Q1" in md
    assert "### A1" in md
    assert "What is 1+1?" in md
    assert "2" in md
    p.unlink()


def test_extract_skips_system():
    records = [
        {"type": "system", "content": "system prompt"},
        {"role": "user", "content": "hi"},
        {"message": {"role": "assistant", "content": "hello"}, "type": ""},
    ]
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
        p = Path(f.name)
    _write_jsonl(p, records)

    md = extract(p)
    assert "system prompt" not in md
    assert "### Q1" in md
    p.unlink()


def test_extract_merges_consecutive():
    records = [
        {"role": "user", "content": "part 1"},
        {"role": "user", "content": "part 2"},
        {"message": {"role": "assistant", "content": "answer"}, "type": ""},
    ]
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
        p = Path(f.name)
    _write_jsonl(p, records)

    md = extract(p)
    # Should merge into one Q, not two
    assert md.count("### Q") == 1
    assert "part 1" in md
    assert "part 2" in md
    p.unlink()


def test_extract_skips_file_history():
    records = [
        {"type": "file-history-snapshot", "content": "big blob"},
        {"role": "user", "content": "question"},
        {"message": {"role": "assistant", "content": "answer"}, "type": ""},
    ]
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
        p = Path(f.name)
    _write_jsonl(p, records)

    md = extract(p)
    assert "big blob" not in md
    assert "### Q1" in md
    p.unlink()


def test_extract_removes_system_reminder_from_content():
    records = [
        {
            "role": "user",
            "content": "hello <system-reminder>hidden</system-reminder> world",
        },
        {"message": {"role": "assistant", "content": "reply"}, "type": ""},
    ]
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
        p = Path(f.name)
    _write_jsonl(p, records)

    md = extract(p)
    assert "hidden" not in md
    assert "hello" in md
    assert "world" in md
    p.unlink()
