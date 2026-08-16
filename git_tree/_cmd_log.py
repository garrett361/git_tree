"""The `log` command: stream a git log graph across all tree-branches."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from git_tree._errors import TreeError
from git_tree._git import _use_color, current_branch, git, git_ok
from git_tree._graph import discover, root_of
from git_tree._registry import subcommand

if TYPE_CHECKING:
    import argparse


@subcommand("log", "Show git log graph for all tree-branches")
def cmd_log(args: argparse.Namespace) -> None:
    graph = discover()
    try:
        branch = current_branch()
    except TreeError:
        print("Not on a tree-branch.")
        raise SystemExit(0) from None
    # A branch participates in the forest if it has a parent (a tracked child) or has
    # children (a root). Anything else is a plain git branch git-tree doesn't track.
    if branch not in graph.parent_of and branch not in graph.children_of:
        print("Not on a tree-branch.")
        raise SystemExit(0)

    root = root_of(graph, branch)
    descendants = graph.downstream_from(root)
    all_refs = [root] + descendants

    cmd = ["git", "log", "--graph", "--oneline", "--decorate"]
    if _use_color():
        cmd.append("--color=always")
    cmd += all_refs

    boundary = git("merge-base", "--octopus", *all_refs, check=False)
    if boundary and git_ok("rev-parse", "--verify", f"{boundary}^"):
        cmd.append(f"^{boundary}^")

    cmd += args.extra
    result = subprocess.run(cmd)
    raise SystemExit(result.returncode)
