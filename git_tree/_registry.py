"""The subcommand registry: what each handler declares about its own subparser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

F = TypeVar("F", bound="Callable[..., object]")


@dataclass(frozen=True)
class Command:
    """One subcommand's declaration: enough for `_build_parser` to create its subparser."""

    name: str
    help: str
    handler: Callable[[argparse.Namespace], object]
    arguments: Callable[[argparse.ArgumentParser], None] | None


COMMANDS: list[Command] = []


def subcommand(
    name: str,
    help: str,
    *,
    arguments: Callable[[argparse.ArgumentParser], None] | None = None,
) -> Callable[[F], F]:
    """Register a handler as a subcommand. Importing the defining module is what runs this,
    which is why `git_tree/__init__.py` imports every command module."""

    def decorate(fn: F) -> F:
        # argparse accepts a duplicate subparser name silently, listing it twice in --help,
        # so catch it here instead.
        if any(c.name == name for c in COMMANDS):
            raise RuntimeError(f"duplicate subcommand: {name}")
        COMMANDS.append(Command(name, help, fn, arguments))
        return fn

    return decorate
