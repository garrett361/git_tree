"""The `branch` command: create or adopt a child branch with a worktree."""

from __future__ import annotations

from typing import TYPE_CHECKING

from git_tree._errors import TreeError
from git_tree._git import (
    _init_submodules_or_warn,
    _is_tree_branch,
    _register_child,
    _set_fork_commit,
    current_branch,
    git,
    git_echo_ok,
    git_ok,
)
from git_tree._graph import discover
from git_tree._guards import _require_worktrees
from git_tree._registry import subcommand
from git_tree._render import _set_completer

if TYPE_CHECKING:
    import argparse


def arguments(p: argparse.ArgumentParser) -> None:
    _set_completer(p.add_argument("path", help="Worktree path for the branch"), "directories")
    p.add_argument("name", help="Branch name (new, or an existing branch to adopt)")
    _set_completer(
        p.add_argument("--parent", help="Base branch to fork from (default: current branch)"),
        "git_heads",
    )
    p.add_argument(
        "--no-submodule-init",
        action="store_true",
        help="Skip automatic `git submodule update --init --recursive` after creating the worktree",
    )


@subcommand(
    "branch",
    "Create or adopt a child branch with a worktree",
    arguments=arguments,
)
def cmd_branch(args: argparse.Namespace) -> None:
    parent = args.parent or current_branch()
    name: str = args.name
    path: str = args.path

    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{parent}"):
        raise TreeError(f"No such branch: {parent}", code=4)
    graph = discover()
    _require_worktrees([parent], graph)

    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
        # New branch: create it at parent's tip.
        if not git_echo_ok("worktree", "add", path, "-b", name, parent):
            raise TreeError(f"failed to create worktree at {path}")
        git("config", f"branch.{name}.tree-parent-branch", parent)
        _set_fork_commit(name, git("rev-parse", parent))
        if not args.no_submodule_init:
            _init_submodules_or_warn(path)
        print(f"Created branch {name} with worktree at {path} (parent: {parent})")
        return

    # Existing branch: adopt it into the tree under parent and give it a worktree.
    # Validate before creating the worktree so a rejected adopt leaves nothing behind;
    # refuse one already in the tree (use plain `git worktree add` for just a worktree,
    # which `git tree` then discovers).
    if name == parent:
        raise TreeError(f"Cannot make {name} its own parent.")
    if _is_tree_branch(name):
        raise TreeError(
            f"{name} is already a tree-branch. Run `git worktree add {path} {name}` to give "
            f"it a worktree (git tree discovers it automatically)."
        )
    base = git("merge-base", parent, name, check=False)
    if not base:
        raise TreeError(f"No common history between {parent} and {name}.")

    if not git_echo_ok("worktree", "add", path, name):
        raise TreeError(f"failed to create worktree at {path}")
    _register_child(name, parent, fork=base)
    if not args.no_submodule_init:
        _init_submodules_or_warn(path)
    print(f"Adopted existing branch {name} with worktree at {path} (parent: {parent})")
