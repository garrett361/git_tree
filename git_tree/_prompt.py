"""Interactive prompts: y/N confirmation and the fzf picker."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from git_tree._errors import ErrorKind, TreeError

if TYPE_CHECKING:
    import argparse


def _prompt(message: str) -> str | None:
    """input() returning the stripped reply, or None on EOF/Ctrl-C (echoing a newline)."""
    try:
        return input(message).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def confirm(message: str) -> bool:
    response = _prompt(f"{message} [y/N] ")
    return response is not None and response.lower() in ("y", "yes")


def _no_input(args: argparse.Namespace) -> bool:
    """True if the tool must never prompt. `--json` (agent mode) implies this: an
    interactive prompt would deadlock an agent that isn't feeding stdin."""
    return args.no_input or args.json


def _require_input(args: argparse.Namespace, what: str, flag: str) -> None:
    """In --no-input mode, refuse to prompt for `what`, naming the `flag` that supplies it."""
    if _no_input(args):
        raise TreeError(
            f"--no-input: {what} required; pass {flag}", code=4, kind=ErrorKind.INPUT_REQUIRED
        )


def _proceed(args: argparse.Namespace, message: str) -> bool:
    """True if the user opted in via --yes or an interactive y/N confirmation."""
    if args.yes:
        return True
    if _no_input(args):
        raise TreeError(
            "confirmation required; pass -y/--yes", code=4, kind=ErrorKind.CONFIRMATION_REQUIRED
        )
    return confirm(message)


# ---------------------------------------------------------------------------
# fzf helpers
# ---------------------------------------------------------------------------


def fzf_select(items: list[str], *, prompt: str = "> ", header: str | None = None) -> list[str]:
    """Single-select via fzf; returns the chosen item as a 0-or-1 element list (empty on
    cancel or when fzf is unavailable). List-valued so callers have one shape to handle."""
    cmd = ["fzf", "--prompt", prompt]
    if header:
        cmd.extend(["--header", header])
    try:
        result = subprocess.run(
            cmd, input="\n".join(items), capture_output=True, text=True, check=True
        )
        return result.stdout.strip().splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _fallback_select(items)


def _fallback_select(items: list[str]) -> list[str]:
    """Numbered-list picker for when fzf isn't installed. One choice, or empty."""
    print("Select:")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    try:
        response = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return []
    if response.isdigit() and 0 <= (idx := int(response) - 1) < len(items):
        return [items[idx]]
    return []


def _select_one(items: list[str], *, prompt: str, header: str) -> str:
    """fzf-pick exactly one item; error (exit 4) if nothing was selected."""
    selected = fzf_select(items, prompt=prompt, header=header)
    if not selected:
        raise TreeError("nothing selected", code=4)
    return selected[0]
