"""Tree rendering: the box-drawing forest view and the JSON forest payload."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from git_tree._git import (
    Color,
    _color,
    _has_active_rebase,
    _worktree_status,
    git,
    git_ok,
)
from git_tree._graph import (
    BranchInfo,
    BranchSnapshot,
    Graph,
    _get_fork_commit,
    _root_remote,
    roots,
)

BOX_PIPE = "│"
BOX_TEE = "├──"
BOX_ELBOW = "└──"
BOX_SPACE = "   "
BOX_PIPE_SPACE = "│  "


def _ahead_behind(branch: str, remote: str | None, worktree: Path) -> tuple[int, int] | None:
    """(ahead, behind) of `branch` vs its remote-tracking ref, or None if no remote ref."""
    remote_ref = f"{remote}/{branch}" if remote else None
    if not remote_ref or not git_ok("rev-parse", "--verify", remote_ref, cwd=worktree):
        return None
    counts = git(
        "rev-list", "--left-right", "--count", f"{branch}...{remote_ref}", cwd=worktree, check=False
    )
    parts = counts.split() if counts else []
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return int(parts[0]), int(parts[1])
    return None


def _git_status_summary(branch: str, info: BranchInfo, remote: str | None) -> str:
    worktree = info.worktree
    if not worktree:
        return ""

    parts: list[str] = []

    snap = info.snapshot
    status = snap.status if snap else _worktree_status(worktree)
    if status.conflicted:
        parts.append(_color(f"✘{status.conflicted}", Color.RED))
    if status.staged:
        parts.append(_color(f"+{status.staged}", Color.GREEN))
    if status.modified:
        parts.append(_color(f"!{status.modified}", Color.RED))
    if status.untracked:
        parts.append(_color(f"?{status.untracked}", Color.RED))

    ab = snap.ahead_behind if snap else _ahead_behind(branch, remote, worktree)
    if ab:
        ahead, behind = ab
        if ahead:
            parts.append(_color(f"⇡{ahead}", Color.GREEN))
        if behind:
            parts.append(_color(f"⇣{behind}", Color.RED))

    if not parts:
        return ""
    return "[" + "".join(parts) + "]"


def _pending_commit_count(parent: str, child: str, info: BranchInfo | None = None) -> int:
    """Count commits on parent that child hasn't incorporated yet.

    Counts from the stored fork point (where child currently sits on parent), so
    the number matches what propagate would actually replay; merge-base would
    over-count once parent and child have drifted.
    """
    if info is not None and info.snapshot is not None:
        return info.snapshot.pending
    base = _get_fork_commit(child, parent, info)
    if not base:
        return 0
    out = git("rev-list", "--count", f"{base}..{parent}", check=False)
    return int(out) if out else 0


def format_tree(
    graph: Graph,
    root: str,
    show_counts: bool = False,
    current: str | None = None,
) -> str:
    marker = "* " if current == root else ""
    lines: list[str] = [f"{marker}{root}"]
    children = graph.children_of.get(root, [])
    # The whole tree shares one remote, anchored at its actual root (which may be above
    # `root` when rendering a mid-tree subtree, e.g. a propagate/rebase preview).
    _, tree_remote = _root_remote(graph, root)
    _format_subtree(
        graph, children, "", lines, show_counts=show_counts, current=current, remote=tree_remote
    )
    return "\n".join(lines)


def _format_subtree(
    graph: Graph,
    children: list[str],
    prefix: str,
    lines: list[str],
    *,
    show_counts: bool = False,
    current: str | None = None,
    remote: str | None = None,
) -> None:
    # The graph is acyclic: discover() prunes any cycle, so this recursion is bounded.
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        connector = BOX_ELBOW if is_last else BOX_TEE

        info = graph.branches.get(child)
        annotation = ""
        if info and info.worktree:
            wt = str(info.worktree).replace(str(Path.home()), "~")
            status = _git_status_summary(child, info, remote)
            status_part = f"  {status}" if status else ""
            annotation = f"  {wt}{status_part}"
        elif info:
            annotation = "  (no worktree)"

        if show_counts:
            parent = graph.parent_of.get(child, "")
            if parent:
                n = _pending_commit_count(parent, child, info)
                if n > 0:
                    annotation += f"  [{n} new]"

        marker = "* " if current == child else ""
        lines.append(f"{prefix}{connector} {marker}{child}{annotation}")

        grandchildren = graph.children_of.get(child, [])
        if grandchildren:
            next_prefix = prefix + (BOX_SPACE if is_last else BOX_PIPE_SPACE)
            _format_subtree(
                graph,
                grandchildren,
                next_prefix,
                lines,
                show_counts=show_counts,
                current=current,
                remote=remote,
            )


def _subtree_lines(graph: Graph, root: str, *, show_counts: bool = False) -> list[str]:
    """The rendered tree rows below `root`, with its header line dropped."""
    return format_tree(graph, root=root, show_counts=show_counts).splitlines()[1:]


def _tree_json(graph: Graph) -> dict:
    """Full-forest machine-readable state: every branch with edges, remote, and worktree
    status. Roots come first, then descendants (topological). See `git tree --json`."""
    ordered: list[str] = []
    seen: set[str] = set()
    for r in roots(graph):
        for b in [r, *graph.downstream_from(r)]:
            if b not in seen:
                seen.add(b)
                ordered.append(b)
    # Broken branches — an orphaned (missing) parent, or a cycle — have their edges dropped, so
    # the walks above miss the childless ones. Surface them too (an agent repairing a tree needs
    # their worktree + status), tagged below so they aren't mistaken for healthy roots.
    # These sets are disjoint: an orphan is never added to parent_of, and _find_cycles only walks
    # parent_of — so a branch can't be both. (Two separate `if`s below are safe either way.)
    orphan_parent = dict(graph.orphans)
    cyclic = {b for cycle in graph.cycles for b in cycle}
    for b in sorted(set(orphan_parent) | cyclic):
        if b not in seen:
            seen.add(b)
            ordered.append(b)

    branches: list[dict] = []
    for name in ordered:
        parent = graph.parent_of.get(name)
        worktree = graph.worktree_of.get(name)
        info = graph.branches.get(name)
        root, remote = _root_remote(graph, name)
        entry: dict = {
            "name": name,
            "parent": parent,
            "children": graph.children_of.get(name, []),
            "root": root,
            "remote": remote,
            "fork_commit": info.fork_commit if info else None,
            "worktree": str(worktree) if worktree else None,
            "dirty": None,
            "staged": None,
            "modified": None,
            "untracked": None,
            "conflicted": None,
            "rebase_in_progress": None,
            "ahead": None,
            "behind": None,
            "pending_from_parent": _pending_commit_count(parent, name, info) if parent else None,
        }
        if worktree:
            snap = info.snapshot if info else None
            st = snap.status if snap else _worktree_status(worktree)
            entry.update(
                dirty=st.dirty,
                staged=st.staged,
                modified=st.modified,
                untracked=st.untracked,
                conflicted=st.conflicted,
                rebase_in_progress=(
                    snap.rebase_in_progress if snap else _has_active_rebase(worktree)
                ),
            )
            ab = snap.ahead_behind if snap else _ahead_behind(name, remote, worktree)
            if ab:
                entry["ahead"], entry["behind"] = ab
        if name in orphan_parent:
            entry["orphaned_parent"] = orphan_parent[name]  # the configured, missing parent
        if name in cyclic:
            entry["cyclic"] = True  # a member of a dependency cycle (see `cycles`)
        branches.append(entry)

    return {
        "roots": roots(graph),
        "cycles": graph.cycles,
        "orphans": [list(o) for o in graph.orphans],
        "branches": branches,
    }


def _hydrate(graph: Graph, branches: list[str]) -> None:
    """Concurrently snapshot each branch's worktree state onto its BranchInfo.

    Read-only: the display and --json paths call this so the per-worktree git calls
    (`status`, ahead/behind, pending-count, rebase check) overlap instead of running
    serially — the dominant cost on slow/networked filesystems, where each `git status`
    is a working-tree walk of stat round-trips. Branches without a worktree are skipped.
    Threads are correct here: every call blocks in `subprocess`, releasing the GIL.
    """
    targets = [
        info for b in branches if (info := graph.branches.get(b)) and info.worktree is not None
    ]
    if not targets:
        return

    def snapshot(info: BranchInfo) -> None:
        wt = info.worktree
        assert wt is not None  # filtered above
        parent = graph.parent_of.get(info.name)
        _, remote = _root_remote(graph, info.name)
        info.snapshot = BranchSnapshot(
            status=_worktree_status(wt),
            ahead_behind=_ahead_behind(info.name, remote, wt),
            pending=_pending_commit_count(parent, info.name, info) if parent else 0,
            rebase_in_progress=_has_active_rebase(wt),
        )

    with ThreadPoolExecutor(max_workers=min(len(targets), 32)) as ex:
        list(ex.map(snapshot, targets))
