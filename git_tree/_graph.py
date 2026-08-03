"""The branch dependency graph: data model, discovery, and root resolution."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from git_tree._git import (
    WorktreeStatus,
    _active_rebase_branch,
    _all_branch_config,
    _branch_remote,
    _worktree_status,
    all_branch_names,
    git,
    git_ok,
)

# Each branch records the commit it forks from its parent in
# branch.<name>.tree-fork-commit. This is the parent's tip the branch was last
# rebased onto (or created/attached at), used as the `--onto <old-base>` exclude
# argument. It is the only reliable fork point once a parent moves ahead of its
# child: merge-base(parent, child) drifts backward in that case.


def _get_fork_commit(branch: str, parent: str, info: BranchInfo | None = None) -> str:
    """Stored fork commit, else a merge-base fallback.

    The stored fork is honored only when it is an ancestor of `branch`, so that the
    `git rebase --onto <parent> <fork>` range (`fork..branch`) is exactly branch's own
    commits. A history rewrite (a manual rebase, or an amend of the fork commit) can
    leave the stored fork off branch's line; merge-base(parent, branch) — always an
    ancestor of branch — is the safe boundary then, as it is for legacy branches.
    """
    if info is not None:
        stored = info.fork_commit
    else:
        stored = git("config", f"branch.{branch}.tree-fork-commit", check=False)
    if (
        stored
        and git_ok("rev-parse", "--verify", stored)
        and git_ok("merge-base", "--is-ancestor", stored, branch)
    ):
        return stored
    # check=False: a branch whose configured parent shares no history (malformed config)
    # has no merge-base; return "" rather than crashing, so read-only paths like `--json`
    # degrade gracefully (callers treat "" as "no fork point").
    return git("merge-base", parent, branch, check=False)


@dataclass
class BranchSnapshot:
    """A read-only, concurrently-gathered snapshot of one worktree's state, produced by
    `_hydrate` for the display/JSON paths so the per-worktree git calls overlap."""

    status: WorktreeStatus
    ahead_behind: tuple[int, int] | None
    pending: int
    rebase_in_progress: bool


@dataclass
class BranchInfo:
    name: str
    worktree: Path | None = None
    fork_commit: str | None = None
    # None until `_hydrate` fills it (display/JSON only). Mutation paths never hydrate, so
    # they always compute live and never read a stale snapshot.
    snapshot: BranchSnapshot | None = None

    @property
    def is_dirty(self) -> bool:
        if self.snapshot is not None:
            return self.snapshot.status.dirty
        return self.worktree is not None and _worktree_status(self.worktree).dirty


@dataclass
class Graph:
    parent_of: dict[str, str] = field(default_factory=dict)
    children_of: dict[str, list[str]] = field(default_factory=dict)
    branches: dict[str, BranchInfo] = field(default_factory=dict)
    # Worktree path per branch for every worktree git knows about, roots included (roots
    # have no BranchInfo, so this is the only place their worktree is recorded).
    worktree_of: dict[str, Path] = field(default_factory=dict)
    # Diagnostics surfaced by discover(): dependency cycles (each a node list) and
    # branches whose configured tree-parent no longer exists.
    cycles: list[list[str]] = field(default_factory=list)
    orphans: list[tuple[str, str]] = field(default_factory=list)

    def downstream_from(self, branch: str) -> list[str]:
        """Return all descendants in topological order (BFS, parents before children)."""
        result: list[str] = []
        queue = list(self.children_of.get(branch, []))
        visited: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            result.append(current)
            queue.extend(self.children_of.get(current, []))
        return result


def roots(graph: Graph) -> list[str]:
    """Every tree root: a tree-branch with children but no tracked parent."""
    return sorted(p for p in graph.children_of if p not in graph.parent_of)


def root_of(graph: Graph, branch: str) -> str:
    """Walk the (functional, acyclic) parent chain up to this branch's root.

    Returns `branch` unchanged when it has no parent — it is itself a root, or it is
    not registered in the tree at all (callers distinguish the two). `discover` prunes
    cycles, so the `seen` guard is purely defensive against a malformed graph.
    """
    seen: set[str] = set()
    while branch in graph.parent_of and branch not in seen:
        seen.add(branch)
        branch = graph.parent_of[branch]
    return branch


def _root_remote(graph: Graph, branch: str) -> tuple[str, str | None]:
    """The tree root for `branch` and that root's configured remote (None if unset).

    A tree has one remote, defined on its root; every branch in the tree pushes there
    and shows ahead/behind against it.
    """
    root = root_of(graph, branch)
    remote = _branch_remote(root) or None
    return root, remote


def _find_cycles(graph: Graph) -> list[list[str]]:
    """Return each parent-chain cycle (in loop order), or [] if the tree is acyclic.

    `tree-parent-branch` config is free-form, so a user can create a cycle
    (A→B→A) or self-parent (A→A). The parent graph is functional (one parent per
    node), so each weakly-connected component has at most one cycle and a simple
    walk finds it.
    """
    cycles: list[list[str]] = []
    seen: set[str] = set()
    for start in graph.parent_of:
        if start in seen:
            continue
        path: list[str] = []
        node = start
        while node in graph.parent_of and node not in seen:
            if node in path:
                cycles.append(path[path.index(node) :])
                break
            path.append(node)
            node = graph.parent_of[node]
        seen.update(path)
    return cycles


def discover() -> Graph:
    graph = Graph()

    worktree_map: dict[str, Path] = {}
    detached_worktrees: list[Path] = []
    porcelain = git("worktree", "list", "--porcelain")
    # Records are separated by a blank line. Parse each independently so the `prunable`
    # marker (which git emits *after* the branch/detached line) is known before we decide
    # what to do with the entry.
    for entry in porcelain.split("\n\n"):
        path: Path | None = None
        branch_name: str | None = None
        detached = False
        prunable = False
        for line in entry.splitlines():
            if line.startswith("worktree "):
                path = Path(line.split(" ", 1)[1])
            elif line.startswith("branch refs/heads/"):
                branch_name = line.removeprefix("branch refs/heads/")
            elif line == "detached":
                detached = True
            elif line.startswith("prunable"):
                prunable = True
        # A prunable worktree's directory is gone (rm -rf'd but not `git worktree prune`d).
        # Skip it entirely: mapping it, or reading its git dir in the recovery loop below,
        # would run git with cwd=<deleted path> and raise FileNotFoundError. Dropping it
        # degrades to "no worktree", which surfaces a clean error downstream.
        if path is None or prunable:
            continue
        if branch_name is not None:
            worktree_map[branch_name] = path
        elif detached:
            # Detached mid-rebase: recover its branch name below. Applies to the primary
            # worktree too (it reports `detached` as the first record), not just linked ones.
            detached_worktrees.append(path)

    # Recover branch names for detached worktrees (mid-rebase)
    for wt_path in detached_worktrees:
        if name := _active_rebase_branch(wt_path):
            worktree_map[name] = wt_path

    graph.worktree_of = worktree_map

    all_branches = all_branch_names()
    all_branches_set = set(all_branches)
    branch_config = _all_branch_config()

    orphaned: list[tuple[str, str]] = []
    for branch in all_branches:
        bc = branch_config.get(branch, {})
        parent = bc.get("tree-parent-branch", "")
        if not parent:
            continue

        if parent not in all_branches_set:
            orphaned.append((branch, parent))
            continue

        fork_commit = bc.get("tree-fork-commit") or None
        info = BranchInfo(
            name=branch,
            worktree=worktree_map.get(branch),
            fork_commit=fork_commit,
        )
        graph.branches[branch] = info
        graph.parent_of[branch] = parent
        graph.children_of.setdefault(parent, []).append(branch)

    graph.orphans = orphaned
    if orphaned:
        lines = [
            "Warning: these branches have a deleted parent "
            "(use `git tree attach` or `git tree detach`):"
        ]
        for b, p in orphaned:
            lines.append(f"  {b}  (parent was: {p})")
        print("\n".join(lines), file=sys.stderr)

    cycles = _find_cycles(graph)
    graph.cycles = cycles
    if cycles:
        lines = [
            "Warning: these branches form a dependency cycle; the cyclic links were dropped "
            "so the rest of the tree still works (fix with `git tree attach`/`git tree detach`):"
        ]
        for cycle in cycles:
            lines.append("  " + " → ".join(cycle + [cycle[0]]))
        print("\n".join(lines), file=sys.stderr)
        # Prune the cyclic edges so an unrelated cycle can't block a healthy tree. Splice
        # each cyclic node out of its parent's children list, then drop its parent edge;
        # keep its `branches` entry. Non-cyclic children of a cyclic node keep valid edges.
        for node in {b for cycle in cycles for b in cycle}:
            parent = graph.parent_of.pop(node, None)
            if parent is not None and node in graph.children_of.get(parent, []):
                graph.children_of[parent].remove(node)
                if not graph.children_of[parent]:
                    del graph.children_of[parent]

    return graph
