#!/usr/bin/env python3
"""Format a one-line shell command into readable multi-line format.

Converts inline env vars to export statements and splits --arguments
onto individual lines with backslash continuations.
"""

import argparse
import re
import sys

ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def tokenize(line: str) -> list[str]:
    """Split a shell line into tokens, respecting quoted strings."""
    tokens: list[str] = []
    current: list[str] = []
    quote_char = None

    for ch in line:
        if quote_char:
            current.append(ch)
            if ch == quote_char:
                quote_char = None
        elif ch in ('"', "'"):
            current.append(ch)
            quote_char = ch
        elif ch == " ":
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)

    if current:
        tokens.append("".join(current))

    return tokens


def extract_env_vars(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Split tokens into leading KEY=VALUE env vars and the rest."""
    for i, tok in enumerate(tokens):
        if not ENV_VAR_RE.match(tok):
            return tokens[:i], tokens[i:]
    return tokens, []


def split_args(tokens: list[str]) -> tuple[list[str], list[list[str]]]:
    """Split tokens into command prefix and --argument groups."""
    cmd_parts: list[str] = []
    arg_groups: list[list[str]] = []

    for tok in tokens:
        if tok.startswith("--"):
            arg_groups.append([tok])
        elif arg_groups:
            arg_groups[-1].append(tok)
        else:
            cmd_parts.append(tok)

    return cmd_parts, arg_groups


def format_command(line: str, indent: int = 2) -> str:
    """Format a single-line shell command into multi-line format."""
    line = line.strip().replace("\\\n", " ")
    if not line:
        return ""

    tokens = tokenize(line)
    env_vars, rest = extract_env_vars(tokens)
    exports = [f"export {v}" for v in env_vars]

    if not rest:
        return "\n".join(exports) + "\n"

    cmd_parts, arg_groups = split_args(rest)
    pad = " " * indent
    arg_lines = [pad + " ".join(g) for g in arg_groups]
    command = " \\\n".join([" ".join(cmd_parts)] + arg_lines)

    if not exports:
        return command + "\n"
    return "\n".join(exports) + "\n\n" + command + "\n"


def format_script(text: str, indent: int = 2) -> str:
    """Format each command line in a shell script."""

    def fmt(line: str) -> str:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return line
        return format_command(line, indent=indent).rstrip("\n")

    return "\n".join(fmt(line) for line in text.splitlines()) + "\n"


def read_input(args) -> str:
    if args.file:
        with open(args.file) as f:
            return f.read()

    if args.write:
        print("error: --write requires a file argument", file=sys.stderr)
        sys.exit(1)

    return sys.stdin.read()


def main():
    parser = argparse.ArgumentParser(
        description="Format a one-line shell command into multi-line format"
    )
    parser.add_argument(
        "file", nargs="?", help="Shell script file (reads stdin if omitted)"
    )
    parser.add_argument(
        "-w", "--write", action="store_true", help="Write output back to file"
    )
    parser.add_argument(
        "-i", "--indent", type=int, default=2, help="Indentation spaces (default: 2)"
    )
    args = parser.parse_args()

    text = read_input(args)
    output = format_script(text, indent=args.indent)

    if args.write:
        with open(args.file, "w") as f:
            f.write(output)
        print(f"Formatted {args.file}", file=sys.stderr)
    else:
        sys.stdout.write(output)


if __name__ == "__main__":
    main()
