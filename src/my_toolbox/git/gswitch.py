"""Git identity switcher — manage multiple git user profiles.

Usage:
    gswitch show                          # show current repo identity
    gswitch list                          # list all profiles
    gswitch use <profile>                 # switch to a profile (local)
    gswitch use <profile> --global        # switch globally
    gswitch add <profile> --name "X" --email "X"
    gswitch remove <profile>
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import typer
import yaml

from my_toolbox.ui import bold, cyan_text, dim, green_text, yellow_text

app = typer.Typer(help="Git identity switcher — manage multiple git user profiles.")

CONFIG_PATH = Path.home() / ".config" / "gswitch" / "profiles.yaml"


@app.callback()
def _callback() -> None:
    """Git identity switcher — manage multiple git user profiles."""


@dataclass
class Profile:
    name: str
    email: str


def _load_profiles() -> Dict[str, Profile]:
    if not CONFIG_PATH.exists():
        return {}
    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {
        key: Profile(name=val["name"], email=val["email"])
        for key, val in raw.get("profiles", {}).items()
    }


def _save_profiles(profiles: Dict[str, Profile]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "profiles": {
            key: {"name": p.name, "email": p.email} for key, p in profiles.items()
        }
    }
    CONFIG_PATH.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True)
    )


def _git_config_get(key: str, *, scope: Optional[str] = None) -> Optional[str]:
    cmd = ["git", "config"]
    if scope:
        cmd.append(f"--{scope}")
    cmd.append(key)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _git_config_set(key: str, value: str, *, is_global: bool = False) -> None:
    cmd = ["git", "config"]
    if is_global:
        cmd.append("--global")
    cmd.extend([key, value])
    subprocess.run(cmd, check=True)


def _match_profile(profiles: Dict[str, Profile], email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    for key, p in profiles.items():
        if p.email == email:
            return key
    return None


def _format_identity(
    name: Optional[str], email: Optional[str], profiles: Dict[str, Profile]
) -> str:
    matched = _match_profile(profiles, email)
    identity = f"{name or '(not set)'} <{email or '(not set)'}>"
    if matched:
        return f"{bold(identity)}  {green_text(matched)}"
    return bold(identity)


@app.command()
def show() -> None:
    """Show the current repo's git identity."""
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    repo_path = (
        toplevel.stdout.strip() if toplevel.returncode == 0 else "(not a git repo)"
    )

    profiles = _load_profiles()

    local_name = _git_config_get("user.name", scope="local")
    local_email = _git_config_get("user.email", scope="local")
    global_name = _git_config_get("user.name", scope="global")
    global_email = _git_config_get("user.email", scope="global")

    typer.echo(f"repo:   {cyan_text(repo_path)}")
    if local_name or local_email:
        typer.echo(f"local:  {_format_identity(local_name, local_email, profiles)}")
    else:
        typer.echo(f"local:  {dim('(not set)')}")
    typer.echo(f"global: {_format_identity(global_name, global_email, profiles)}")


@app.command("list")
def list_profiles() -> None:
    """List all configured profiles."""
    profiles = _load_profiles()
    if not profiles:
        typer.echo("No profiles configured. Use 'gswitch add' to create one.")
        raise typer.Exit()

    cur_email = _git_config_get("user.email")
    matched = _match_profile(profiles, cur_email)

    for key, p in profiles.items():
        marker = green_text("* ") if key == matched else "  "
        label = bold(key)
        typer.echo(f"{marker}{label}  {p.name} <{p.email}>")


@app.command()
def use(
    profile: str = typer.Argument(help="Profile name to switch to"),
    is_global: bool = typer.Option(False, "--global", "-g", help="Set globally"),
) -> None:
    """Switch git identity to a profile."""
    profiles = _load_profiles()
    if profile not in profiles:
        typer.echo(f"error: profile '{profile}' not found", err=True)
        typer.echo(f"available: {', '.join(profiles.keys())}", err=True)
        raise typer.Exit(1)

    p = profiles[profile]
    scope = "global" if is_global else "local"
    _git_config_set("user.name", p.name, is_global=is_global)
    _git_config_set("user.email", p.email, is_global=is_global)
    typer.echo(f"Switched to {green_text(profile)} ({scope}): {p.name} <{p.email}>")


@app.command()
def add(
    profile: str = typer.Argument(help="Profile name"),
    name: str = typer.Option(..., "--name", "-n", help="Git user.name"),
    email: str = typer.Option(..., "--email", "-e", help="Git user.email"),
) -> None:
    """Add a new profile."""
    profiles = _load_profiles()
    if profile in profiles:
        typer.echo(
            f"Profile '{profile}' already exists. Use 'gswitch remove' first.",
            err=True,
        )
        raise typer.Exit(1)

    profiles[profile] = Profile(name=name, email=email)
    _save_profiles(profiles)
    typer.echo(f"Added profile {green_text(profile)}: {name} <{email}>")


@app.command()
def remove(
    profile: str = typer.Argument(help="Profile name to remove"),
) -> None:
    """Remove a profile."""
    profiles = _load_profiles()
    if profile not in profiles:
        typer.echo(f"error: profile '{profile}' not found", err=True)
        raise typer.Exit(1)

    del profiles[profile]
    _save_profiles(profiles)
    typer.echo(f"Removed profile {yellow_text(profile)}")


if __name__ == "__main__":
    app()
