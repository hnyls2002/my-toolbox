#!/usr/bin/env python3
"""Extract a Claude Code session JSONL into a readable Markdown Q&A log.

Filters out tool calls, system reminders, file-history snapshots, and other
non-visible messages. Consecutive same-role messages are merged. Output is
paired Q&A sections with sequential numbering.

Usage:
    extract-claude-chat <session.jsonl> [-o output.md]
    extract-claude-chat <session.jsonl>              # prints to stdout
"""

import json
import re
import sys
from pathlib import Path

import typer

app = typer.Typer(add_completion=False)

# Message types that are never visible in the Claude Code UI
_SKIP_TYPES = frozenset({"file-history-snapshot", "system", "queue-operation"})

# Standalone content that should be dropped
_SKIP_CONTENT = frozenset(
    {
        "No response requested.",
        "[Request interrupted by user]",
        "[Request interrupted by user for tool use]",
    }
)


def _extract_text(obj: dict) -> str:
    """Pull visible text out of a JSONL message object."""
    parts: list[str] = []

    def _collect(c):
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))

    if "message" in obj and isinstance(obj["message"], dict):
        _collect(obj["message"].get("content", ""))
    else:
        _collect(obj.get("content", ""))

    return "\n".join(parts).strip()


def _get_role(obj: dict) -> str:
    if isinstance(obj.get("message"), dict):
        return obj["message"].get("role", obj.get("role", ""))
    return obj.get("role", "")


def _clean(text: str) -> str:
    """Remove system-reminder blocks and other non-visible markup."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    return text.strip()


def _should_skip(content: str) -> bool:
    if not content:
        return True
    if content in _SKIP_CONTENT:
        return True
    if any(
        tag in content
        for tag in (
            "<local-command-caveat>",
            "<command-name>",
            "<local-command-stdout>",
        )
    ):
        return True
    if content.startswith(
        "This session is being continued from a previous conversation"
    ):
        return True
    return False


def extract(jsonl_path: Path) -> str:
    """Parse a JSONL session file and return Markdown."""
    messages: list[tuple[str, str]] = []

    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("type", "") in _SKIP_TYPES:
                continue

            role = _get_role(obj)
            if role not in ("user", "assistant"):
                continue

            content = _clean(_extract_text(obj))
            if _should_skip(content):
                continue

            messages.append((role, content))

    # Merge consecutive same-role messages
    merged: list[tuple[str, str]] = []
    for role, content in messages:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + "\n\n" + content)
        else:
            merged.append((role, content))

    # Build markdown
    session_id = jsonl_path.stem
    lines = [
        f"# Claude Code Session Chat Log",
        "",
        f"> **Session:** `{session_id}`",
        "",
        "---",
        "",
    ]

    pair_num = 0
    i = 0
    while i < len(merged):
        role, content = merged[i]
        if role == "user":
            pair_num += 1
            lines.append(f"### Q{pair_num}")
            lines.append("")
            lines.append(content)
            lines.append("")
            if i + 1 < len(merged) and merged[i + 1][0] == "assistant":
                lines.append(f"### A{pair_num}")
                lines.append("")
                lines.append(merged[i + 1][1])
                lines.append("")
                lines.append("---")
                lines.append("")
                i += 2
                continue
            else:
                lines.append("---")
                lines.append("")
        else:
            pair_num += 1
            lines.append(f"### A{pair_num} (continued)")
            lines.append("")
            lines.append(content)
            lines.append("")
            lines.append("---")
            lines.append("")
        i += 1

    return "\n".join(lines)


@app.command()
def main(
    jsonl_path: Path = typer.Argument(
        ..., help="Path to Claude Code session .jsonl file"
    ),
    output: Path = typer.Option(
        None, "-o", "--output", help="Output .md file (default: stdout)"
    ),
):
    """Extract a Claude Code session JSONL into readable Markdown Q&A."""
    if not jsonl_path.exists():
        typer.echo(f"Error: {jsonl_path} not found", err=True)
        raise typer.Exit(1)

    md = extract(jsonl_path)

    if output:
        output.write_text(md)
        typer.echo(f"Saved to {output} ({md.count('### Q')} Q&A pairs)")
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    app()
