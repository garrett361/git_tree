"""The default forest view: print the branch tree, or its JSON payload."""

from __future__ import annotations

from typing import TYPE_CHECKING

from git_tree._display import _hydrate, _tree_json, format_tree
from git_tree._git import git
from git_tree._graph import discover, root_of, roots

if TYPE_CHECKING:
    import argparse


def cmd_tree(args: argparse.Namespace) -> dict | None:
    graph = discover()
    if args.json:
        _hydrate(graph, list(graph.branches))
        # Always the full forest, regardless of current branch or --all: an agent querying
        # state usually isn't "on" a tree branch, and JSON has no clutter cost. Returned (not
        # printed) so main() wraps it in the envelope and writes it to the real stdout.
        return _tree_json(graph)
    raw = git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    current = None if (not raw or raw == "HEAD") else raw

    all_roots = roots(graph)
    if args.all:
        # A root is a tree-branch with children but no tracked parent; show every one,
        # including stacks whose base isn't main (otherwise invisible).
        to_show = all_roots
    elif current and (current in graph.parent_of or current in graph.children_of):
        # Default: just the tree containing the current branch.
        to_show = [root_of(graph, current)]
    else:
        to_show = []

    if to_show:
        rendered = [b for r in to_show for b in (r, *graph.downstream_from(r))]
        _hydrate(graph, rendered)
        blocks = [format_tree(graph, root=r, current=current, show_counts=True) for r in to_show]
        print("\n\n".join(blocks))
    elif not all_roots:
        print("  (no tree-branches registered — use `git tree attach` or `git tree branch`)")
    else:
        print("Not on a tree-branch. Use `git tree --all` to see all trees.")
