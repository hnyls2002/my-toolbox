"""Markdown preview session manager — local rendering, no GitHub API needed.

Usage:
    rgrip open <file>          # open a markdown file (auto-assign port)
    rgrip open <file> -p 6420  # open on a specific port
    rgrip list                 # list all active sessions
    rgrip stop <file|port>     # stop a specific session
    rgrip stop --all           # stop all sessions
    rgrip browse <file|port>   # open session in browser
"""

import builtins
import os
import re
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Optional

builtins_open = builtins.open

import typer

from my_toolbox.ui import cyan_text, dim, green_text, red_text, section_header

app = typer.Typer(help="Markdown preview session manager.")

BASE_PORT = 6419
MAX_PORT = 6439
PROCESS_TAG = "my_toolbox.grip.server"


def _find_sessions() -> list[dict]:
    """Find all running markdown preview server processes."""
    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError:
        return []

    sessions = []
    for line in result.stdout.splitlines():
        if PROCESS_TAG not in line or "grep" in line:
            continue
        parts = line.split()
        pid = int(parts[1])

        # Find the server module args: ... my_toolbox.grip.server <file> <port>
        try:
            tag_idx = next(i for i, p in enumerate(parts) if PROCESS_TAG in p)
        except StopIteration:
            continue

        args = parts[tag_idx + 1 :]
        if len(args) < 2:
            continue

        file_path = args[0]
        try:
            port = int(args[1])
        except ValueError:
            continue

        sessions.append({"pid": pid, "file": file_path, "port": port})
    return sessions


def _used_ports() -> set[int]:
    return {s["port"] for s in _find_sessions()}


def _next_port() -> int:
    used = _used_ports()
    for port in range(BASE_PORT, MAX_PORT + 1):
        if port not in used:
            return port
    typer.echo(red_text(f"No available ports in range {BASE_PORT}-{MAX_PORT}"))
    raise typer.Exit(1)


def _resolve_file(file_path: str) -> Path:
    p = Path(file_path).resolve()
    if not p.exists():
        typer.echo(red_text(f"File not found: {p}"))
        raise typer.Exit(1)
    if p.suffix.lower() not in (".md", ".markdown", ".rst", ".txt"):
        typer.echo(red_text(f"Not a markdown file: {p}"))
        raise typer.Exit(1)
    return p


def _find_session(target: str) -> Optional[dict]:
    sessions = _find_sessions()
    if target.isdigit():
        port = int(target)
        for s in sessions:
            if s["port"] == port:
                return s
    for s in sessions:
        if target in s["file"]:
            return s
    return None


@app.command()
def open(
    file: str = typer.Argument(..., help="Markdown file to preview"),
    port: Optional[int] = typer.Option(None, "-p", "--port", help="Port number"),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Host to bind (use 0.0.0.0 for network access)"
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="Skip opening browser"),
):
    """Open a markdown file as a background preview session (opens browser on first launch)."""
    resolved = _resolve_file(file)

    for s in _find_sessions():
        if str(resolved) == s["file"]:
            typer.echo(
                f"Already open: {cyan_text(s['file'])} on port {green_text(str(s['port']))}"
            )
            return

    if port is None:
        port = _next_port()

    typer.echo(f"Opening {cyan_text(str(resolved))} on port {green_text(str(port))}")

    log_dir = Path.home() / ".cache" / "rgrip"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"rgrip-{port}.log"

    cmd = [sys.executable, "-m", PROCESS_TAG, str(resolved), str(port), host]

    with builtins_open(log_file, "w") as f:
        subprocess.Popen(cmd, stdout=f, stderr=f, start_new_session=True)

    url = f"http://localhost:{port}"
    typer.echo(green_text(f"Session started — {url}"))
    if not no_browser:
        webbrowser.open(url)


def _extract_title(file_path: str) -> str:
    """Extract the first markdown heading from a file as title."""
    try:
        text = Path(file_path).read_text(encoding="utf-8")
        m = re.search(r"^#{1,3}\s+(.+)$", text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return Path(file_path).name


@app.command("list")
def list_sessions():
    """List all active preview sessions."""
    sessions = _find_sessions()
    if not sessions:
        typer.echo(dim("No active sessions."))
        return

    typer.echo(section_header("Preview Sessions"))
    for s in sessions:
        title = _extract_title(s["file"])
        url = f"http://localhost:{s['port']}"
        typer.echo(
            f"  {cyan_text(title)}  {dim(s['file'])}"
            f"\n    {green_text(url)}  (pid {dim(str(s['pid']))})"
        )
    typer.echo(dim(f"\n  {len(sessions)} session(s) active"))


@app.command()
def stop(
    target: Optional[str] = typer.Argument(None, help="File path or port to stop"),
    all_sessions: bool = typer.Option(False, "--all", "-a", help="Stop all sessions"),
):
    """Stop a preview session by file/port, or all sessions."""
    if all_sessions:
        sessions = _find_sessions()
        if not sessions:
            typer.echo(dim("No active sessions."))
            return
        for s in sessions:
            os.kill(s["pid"], signal.SIGTERM)
            typer.echo(f"Stopped: {cyan_text(s['file'])} (pid {s['pid']})")
        return

    if target is None:
        typer.echo(red_text("Specify a file/port or use --all"))
        raise typer.Exit(1)

    session = _find_session(target)
    if session is None:
        typer.echo(red_text(f"No session found matching: {target}"))
        raise typer.Exit(1)

    os.kill(session["pid"], signal.SIGTERM)
    typer.echo(f"Stopped: {cyan_text(session['file'])} (pid {session['pid']})")


@app.command()
def browse(
    target: str = typer.Argument(..., help="File path or port to open in browser"),
):
    """Open an active session in the browser."""
    session = _find_session(target)
    if session is None:
        typer.echo(red_text(f"No session found matching: {target}"))
        raise typer.Exit(1)

    url = f"http://localhost:{session['port']}"
    typer.echo(f"Opening {cyan_text(url)}")
    webbrowser.open(url)


if __name__ == "__main__":
    app()
