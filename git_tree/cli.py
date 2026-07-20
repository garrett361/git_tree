"""git-tree: Cascading rebase tool for branch dependency chains."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import NoReturn


class TreeError(SystemExit):
    """Raised by helpers to exit with a user-facing message.

    `code` is the process exit status, letting an agent branch on failure class:
    1 generic, 3 resumable conflict, 4 precondition/state, 5 not-a-tree-branch.

    The message is printed to stderr as a human diagnostic (both modes). In `--json`
    mode `main()` also renders these fields into an error envelope on stdout: `kind` is a
    stable machine tag (defaults to a code-derived value), `branches` names the offending
    branches, and `remedy` is an argv list the agent can run directly.
    """

    def __init__(
        self,
        msg: str,
        code: int = 1,
        *,
        kind: str | None = None,
        branches: list[str] | None = None,
        remedy: list[str] | None = None,
    ):
        print(msg, file=sys.stderr)
        self.message = msg
        self.kind = kind
        self.branches = branches
        self.remedy = remedy
        super().__init__(code)


class ConflictError(TreeError):
    """A resumable rebase conflict (exit 3). Carries the stuck branch, its worktree, and the
    unmerged files so an agent can resolve and resume without parsing prose."""

    def __init__(
        self,
        msg: str,
        *,
        branch: str,
        worktree: Path,
        conflicted_files: list[str],
        remedy: list[str] | None = None,
    ):
        super().__init__(msg, code=3, kind="conflict", remedy=remedy)
        self.branch = branch
        self.worktree = worktree
        self.conflicted_files = conflicted_files


def _version() -> str:
    try:
        return metadata.version("git-tree")
    except metadata.PackageNotFoundError:
        return "0+unknown"


# ---------------------------------------------------------------------------
# Color
# ---------------------------------------------------------------------------


class Color(StrEnum):
    RED = "31"
    GREEN = "32"
    DIM = "2"


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _color(text: str, code: Color) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run(
    *args: str,
    check: bool = True,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, **env} if env else None,
    )


def git(*args: str, cwd: Path | str | None = None, check: bool = True) -> str:
    result = _run("git", *args, cwd=cwd, check=check)
    return result.stdout.strip()


def git_lines(*args: str, cwd: Path | str | None = None) -> list[str]:
    out = git(*args, cwd=cwd)
    return out.splitlines() if out else []


def git_ok(*args: str, cwd: Path | str | None = None) -> bool:
    result = _run("git", *args, check=False, cwd=cwd)
    return result.returncode == 0


def git_echo(
    *args: str, cwd: Path | str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a side-effecting git command, echoing the invocation and reprinting its output.

    Captures (rather than streams) so callers keep stdout/stderr for logic and so the
    output is visible the same way regardless of TTY. Returns the completed process.
    """
    print(_color(f"+ git {' '.join(args)}", Color.DIM))
    result = _run("git", *args, check=False, cwd=cwd, env=env)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result


def git_echo_ok(
    *args: str, cwd: Path | str | None = None, env: dict[str, str] | None = None
) -> bool:
    """`git_echo` for callers that only need success/failure (mirrors `git_ok`)."""
    return git_echo(*args, cwd=cwd, env=env).returncode == 0


def _git_dir(cwd: Path) -> Path:
    """Absolute path to the git dir of the working tree containing `cwd`."""
    git_dir = git("rev-parse", "--git-dir", cwd=cwd)
    return Path(git_dir) if Path(git_dir).is_absolute() else cwd / git_dir


def _is_conflict(xy: str) -> bool:
    """True if a porcelain status XY pair marks an unmerged (conflicted) path."""
    return "U" in xy or xy in ("DD", "AA")


@dataclass(frozen=True)
class WorktreeStatus:
    """Tallied `git status --porcelain` XY codes for a worktree."""

    staged: int
    modified: int
    untracked: int
    conflicted: int
    dirty: bool  # any porcelain output at all


def _worktree_status(wt: Path) -> WorktreeStatus:
    """Run `git status --porcelain` once and tally its XY codes."""
    out = git("status", "--porcelain", cwd=wt)
    staged = modified = untracked = conflicted = 0
    for line in out.splitlines():
        xy = line[:2]
        x, y = xy[0], xy[1]
        if _is_conflict(xy):
            conflicted += 1
        elif x in "MADRCT":  # include T (type-change), e.g. file <-> symlink
            staged += 1
        if y == "?":
            untracked += 1
        elif y not in (" ", "!", "U"):
            modified += 1
    return WorktreeStatus(staged, modified, untracked, conflicted, dirty=bool(out))


# ---------------------------------------------------------------------------
# Fork point storage
# ---------------------------------------------------------------------------

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


def _set_fork_commit(branch: str, commit: str) -> None:
    git("config", f"branch.{branch}.tree-fork-commit", commit)


# ---------------------------------------------------------------------------
# Tree edge storage
# ---------------------------------------------------------------------------


def _get_tree_parent(branch: str) -> str:
    """Tree parent branch, or "" if unset."""
    return git("config", f"branch.{branch}.tree-parent-branch", check=False)


def _unset_tree_config(branch: str) -> None:
    """Remove all tree config for a branch: parent edge and fork commit."""
    for key in ("tree-parent-branch", "tree-fork-commit"):
        git("config", "--unset", f"branch.{branch}.{key}", check=False)


def _would_cycle(branch: str, new_parent: str) -> bool:
    """True if making `new_parent` the tree-parent of `branch` would close a cycle.

    Setting that edge points `branch` up at `new_parent`, so it loops iff `branch` is
    already at or above `new_parent`: walk `new_parent`'s parent chain and stop if it
    reaches `branch` (`new_parent == branch` is the self-parent case). Reads config
    directly so it works before the edge exists and even from an already-cyclic state;
    `seen` bounds the walk against a pre-existing cycle.
    """
    node = new_parent
    seen: set[str] = set()
    while node and node not in seen:
        if node == branch:
            return True
        seen.add(node)
        node = _get_tree_parent(node)
    return False


def _register_child(child: str, parent: str, *, fork: str | None = None) -> None:
    """Register `child` under `parent`: write the tree-parent edge and the fork point
    (the merge-base, where propagate/rebase replay from), warning if `child` doesn't
    descend from `parent` and raising if they share no history. Pass `fork` to reuse an
    already-computed merge-base. Callers handle the self/cycle checks."""
    base = fork or git("merge-base", parent, child, check=False)
    if not base:
        raise TreeError(f"No common history between {parent} and {child}.")
    is_ancestor = git_ok("merge-base", "--is-ancestor", parent, child)
    if not is_ancestor and base != git("rev-parse", parent):
        print(f"Warning: {child} does not appear to descend from {parent}.", file=sys.stderr)
    git("config", f"branch.{child}.tree-parent-branch", parent)
    _set_fork_commit(child, base)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BranchInfo:
    name: str
    worktree: Path | None = None
    fork_commit: str | None = None

    @property
    def is_dirty(self) -> bool:
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


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def current_branch() -> str:
    proc = _run("git", "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if proc.returncode != 0:
        msg = proc.stderr.strip() if proc.stderr else "not on a branch"
        raise TreeError(f"fatal: {msg}")
    name = proc.stdout.strip()
    if name == "HEAD":
        # Detached HEAD: rev-parse --abbrev-ref prints the literal "HEAD".
        # Don't let callers operate on (or write config for) a branch named HEAD.
        raise TreeError("fatal: not on a branch (detached HEAD)")
    return name


def all_branch_names() -> list[str]:
    """All local branch names (refs/heads), in git's default ordering."""
    return git_lines("for-each-ref", "--format=%(refname:short)", "refs/heads/")


def _is_tree_branch(name: str) -> bool:
    """True if `name` already participates in the tree, as a tracked child (it has a
    tree-parent) or as some branch's tree-parent (a root or interior node)."""
    return bool(_get_tree_parent(name)) or any(
        _get_tree_parent(b) == name for b in all_branch_names()
    )


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


def _branch_remote(branch: str) -> str:
    """The branch's configured remote (branch.<name>.remote), or "" if unset."""
    return git("config", f"branch.{branch}.remote", check=False)


def _set_branch_remote(branch: str, remote: str) -> None:
    git("config", f"branch.{branch}.remote", remote)


def _root_remote(graph: Graph, branch: str) -> tuple[str, str | None]:
    """The tree root for `branch` and that root's configured remote (None if unset).

    A tree has one remote, defined on its root; every branch in the tree pushes there
    and shows ahead/behind against it.
    """
    root = root_of(graph, branch)
    remote = _branch_remote(root) or None
    return root, remote


def _carry_remote_to_root(old_root: str, new_root: str) -> None:
    """Carry a tree's remote anchor (branch.<root>.remote) onto a new root.

    A tree has one remote, on its root; an operation that inserts a new root above the
    old one (root split, or rebase onto a branch outside the tree) must carry the anchor
    or push can no longer resolve it. No-op when the roots match, the old root had no
    remote, or the new root already has its own (it brought its own tree's remote). The
    old root keeps its key: it may still root sibling branches, and push reads only the
    root's.
    """
    if old_root == new_root:
        return
    old_remote = _branch_remote(old_root)
    if not old_remote or _branch_remote(new_root):
        return
    _set_branch_remote(new_root, old_remote)
    print(f"Carried tree remote '{old_remote}' to new root '{new_root}'.")


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
        git_dir_path = _git_dir(wt_path)
        head_name_file = git_dir_path / "rebase-merge" / "head-name"
        if not head_name_file.exists():
            head_name_file = git_dir_path / "rebase-apply" / "head-name"
        if head_name_file.exists():
            ref = head_name_file.read_text().strip()
            if ref.startswith("refs/heads/"):
                worktree_map[ref.removeprefix("refs/heads/")] = wt_path

    graph.worktree_of = worktree_map

    all_branches = all_branch_names()
    all_branches_set = set(all_branches)

    orphaned: list[tuple[str, str]] = []
    for branch in all_branches:
        parent = _get_tree_parent(branch)
        if not parent:
            continue

        if parent not in all_branches_set:
            orphaned.append((branch, parent))
            continue

        fork_commit = git("config", f"branch.{branch}.tree-fork-commit", check=False) or None
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


# ---------------------------------------------------------------------------
# Tree display
# ---------------------------------------------------------------------------

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

    status = _worktree_status(worktree)
    if status.conflicted:
        parts.append(_color(f"✘{status.conflicted}", Color.RED))
    if status.staged:
        parts.append(_color(f"+{status.staged}", Color.GREEN))
    if status.modified:
        parts.append(_color(f"!{status.modified}", Color.RED))
    if status.untracked:
        parts.append(_color(f"?{status.untracked}", Color.RED))

    ab = _ahead_behind(branch, remote, worktree)
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


def _print_results(results: list[tuple[str, str]]) -> None:
    print("Results:")
    for name, status in results:
        print(f"  {name}: {status}")


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


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
    return getattr(args, "no_input", False) or getattr(args, "json", False)


def _require_input(args: argparse.Namespace, what: str, flag: str) -> None:
    """In --no-input mode, refuse to prompt for `what`, naming the `flag` that supplies it."""
    if _no_input(args):
        raise TreeError(f"--no-input: {what} required; pass {flag}", code=4, kind="input_required")


def _proceed(args: argparse.Namespace, message: str) -> bool:
    """True if the user opted in via --yes or an interactive y/N confirmation."""
    if getattr(args, "yes", False):
        return True
    if _no_input(args):
        raise TreeError(
            "confirmation required; pass -y/--yes", code=4, kind="confirmation_required"
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


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


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
    # Safety net for any node reachable only via edges but not from a root.
    for b in sorted(set(graph.parent_of) | set(graph.children_of)):
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
        _, remote = _root_remote(graph, name)
        entry: dict = {
            "name": name,
            "parent": parent,
            "children": graph.children_of.get(name, []),
            "root": root_of(graph, name),
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
            st = _worktree_status(worktree)
            entry.update(
                dirty=st.dirty,
                staged=st.staged,
                modified=st.modified,
                untracked=st.untracked,
                conflicted=st.conflicted,
                rebase_in_progress=_has_active_rebase(worktree),
            )
            ab = _ahead_behind(name, remote, worktree)
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


def cmd_tree(args: argparse.Namespace) -> dict | None:
    graph = discover()
    if getattr(args, "json", False):
        # Always the full forest, regardless of current branch or --all: an agent querying
        # state usually isn't "on" a tree branch, and JSON has no clutter cost. Returned (not
        # printed) so main() wraps it in the envelope and writes it to the real stdout.
        return _tree_json(graph)
    raw = git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    current = None if (not raw or raw == "HEAD") else raw

    all_roots = roots(graph)
    if getattr(args, "all", False):
        # A root is a tree-branch with children but no tracked parent; show every one,
        # including stacks whose base isn't main (otherwise invisible).
        to_show = all_roots
    elif current and (current in graph.parent_of or current in graph.children_of):
        # Default: just the tree containing the current branch.
        to_show = [root_of(graph, current)]
    else:
        to_show = []

    if to_show:
        blocks = [format_tree(graph, root=r, current=current, show_counts=True) for r in to_show]
        print("\n\n".join(blocks))
    elif not all_roots:
        print("  (no tree-branches registered — use `git tree attach` or `git tree branch`)")
    else:
        print("Not on a tree-branch. Use `git tree --all` to see all trees.")


def cmd_branch(args: argparse.Namespace) -> None:
    parent = current_branch()
    name: str = args.name
    path: str = args.path

    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
        # New branch: create it at the current tip, parented here.
        if not git_echo_ok("worktree", "add", path, "-b", name):
            raise TreeError(f"failed to create worktree at {path}")
        git("config", f"branch.{name}.tree-parent-branch", parent)
        _set_fork_commit(name, git("rev-parse", parent))
        if not getattr(args, "no_submodule_init", False):
            _init_submodules_or_warn(path)
        print(f"Created branch {name} with worktree at {path} (parent: {parent})")
        return

    # Existing branch: adopt it into the tree under the current branch and give it a
    # worktree. Validate before creating the worktree so a rejected adopt leaves nothing
    # behind; refuse one already in the tree (use plain `git worktree add` for just a
    # worktree, which `git tree` then discovers).
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
    if not getattr(args, "no_submodule_init", False):
        _init_submodules_or_warn(path)
    print(f"Adopted existing branch {name} with worktree at {path} (parent: {parent})")


def cmd_attach(args: argparse.Namespace) -> None:
    branch = current_branch()
    parent: str | None = args.parent

    if not parent:
        _require_input(args, "parent branch", "the parent argument")
        candidates = [b for b in all_branch_names() if b != branch]
        if not candidates:
            raise TreeError("No other branches available.")
        parent = _select_one(candidates, prompt="Select parent> ", header="Choose parent branch")

    if parent == branch:
        raise TreeError(f"Cannot attach {branch} to itself.")
    if _would_cycle(branch, parent):
        raise TreeError(
            f"Cannot attach {branch} to {parent}: {parent} descends from {branch} "
            f"in the tree (would create a cycle)."
        )

    _register_child(branch, parent)
    print(f"Attached {branch} to {parent}")


def cmd_detach(args: argparse.Namespace) -> None:
    branch = getattr(args, "branch", None) or current_branch()
    parent = _get_tree_parent(branch)
    if not parent:
        raise TreeError(f"{branch} is not in the tree.", code=5)

    # detach is the recovery path for a hand-edited cyclic config, so it must work even
    # when discover() refuses to build a graph. Fall back to a config-only child lookup
    # and skip the subtree preview (which can't safely render a cyclic graph).
    try:
        graph: Graph | None = discover()
    except TreeError:
        graph = None

    if graph is not None:
        children = graph.children_of.get(branch, [])
    else:
        children = [b for b in all_branch_names() if _get_tree_parent(b) == branch]

    print(f"Detaching {branch} from {parent}.")
    if children:
        print(f"{branch} has children — they will form a separate tree:")
        if graph is not None:
            print(format_tree(graph, root=branch))
        else:
            for child in children:
                print(f"  {child}")

    if not _proceed(args, "Proceed?"):
        return

    _unset_tree_config(branch)
    print(f"Detached {branch} (was child of {parent})")

    if children:
        try:
            graph = discover()
        except TreeError:
            return
        other_roots = [r for r in roots(graph) if r != branch]
        if other_roots:
            print("\nRemaining tree(s):")
            print("\n\n".join(format_tree(graph, root=r) for r in other_roots))


def cmd_remove(args: argparse.Namespace) -> None:
    """Tear down a subtree's worktrees and unregister its branches from the tree.

    This removes worktree directories and unsets tree config; it never deletes a branch
    ref, so no committed work can be lost. The only data at risk is uncommitted changes,
    which the clean-worktree gate protects.
    """
    graph = discover()
    try:
        cur: str | None = current_branch()
    except TreeError:
        cur = None

    target = getattr(args, "branch", None)
    if target is None:
        _require_input(args, "branch to remove", "the branch argument")
        # No branch given: pick from removable tree-branches that have a worktree. The
        # picker doesn't pre-filter dirty ones — the clean gate below still catches them.
        candidates = sorted(
            b
            for b in graph.parent_of
            if b != cur and (info := graph.branches.get(b)) and info.worktree
        )
        if not candidates:
            raise TreeError("No tree-branch worktrees available to remove.")
        target = _select_one(
            candidates,
            prompt="Remove worktree> ",
            header="Select a tree-branch to remove (its worktree + subtree)",
        )

    # Only non-root tree-branches: this never touches a tree's trunk / main worktree.
    if target not in graph.parent_of:
        raise TreeError(
            f"{target} is not a removable tree-branch — it has no tree-parent "
            f"(git tree remove won't touch a tree root).",
            code=5,
        )

    subtree = [target] + graph.downstream_from(target)  # parents-first

    if cur in subtree:
        raise TreeError(
            f"cannot remove {cur}: it's the branch you're on. "
            f"Switch to a branch outside the subtree first."
        )

    # Safety gate (all-or-nothing): never remove a worktree with uncommitted work. Branch
    # refs are kept, so committed work is never at risk — only this needs checking.
    dirty = [b for b in subtree if (info := graph.branches.get(b)) and info.is_dirty]
    if dirty:
        lines = ["Refusing to remove — these worktrees have uncommitted changes:"]
        lines += [f"  {b}  ({graph.branches[b].worktree})" for b in dirty]
        lines.append("\nCommit, stash, or discard them first. Nothing was removed.")
        raise TreeError("\n".join(lines), code=4, branches=dirty)

    print(f"Removing worktrees and unregistering {target} + its subtree (branch refs kept):")
    print(format_tree(graph, root=target))
    print()
    if getattr(args, "dry_run", False):
        return
    if not _proceed(args, "Remove these worktrees and detach the branches?"):
        return

    # Children-first; stop on the first git failure rather than report false success.
    removed_worktrees = 0
    for b in reversed(subtree):
        info = graph.branches.get(b)
        if info and info.worktree:
            if not git_echo_ok("worktree", "remove", str(info.worktree)):
                raise TreeError(f"failed to remove {b}'s worktree — stopping (see output above).")
            removed_worktrees += 1
        _unset_tree_config(b)

    print(
        f"\nDetached {len(subtree)} branch(es) from the tree; "
        f"removed {removed_worktrees} worktree(s). Branch refs kept."
    )


def _prunable_worktree_path(branch: str) -> Path | None:
    """Path of a stale (prunable) worktree registration for `branch`, if any.

    Its directory was deleted (rm -rf'd) without `git worktree prune`, so `discover()`
    drops it and the branch looks worktree-less. `cmd_repair` uses this to point the user
    at recovery rather than a bare "nothing to repair"."""
    porcelain = git("worktree", "list", "--porcelain")
    for entry in porcelain.split("\n\n"):
        lines = entry.splitlines()
        if not any(line == f"branch refs/heads/{branch}" for line in lines):
            continue
        if not any(line.startswith("prunable") for line in lines):
            continue
        path = next((line.split(" ", 1)[1] for line in lines if line.startswith("worktree ")), None)
        if path:
            return Path(path)
    return None


def cmd_repair(args: argparse.Namespace) -> None:
    """Nuke and recreate a corrupted worktree, preserving branch ref and tree config."""
    graph = discover()

    target = getattr(args, "branch", None)
    if target is None:
        _require_input(args, "branch to repair", "the branch argument")
        candidates = sorted(
            b for b in graph.parent_of if (info := graph.branches.get(b)) and info.worktree
        )
        if not candidates:
            raise TreeError("No tree-branch worktrees available to repair.")
        target = _select_one(
            candidates,
            prompt="Repair worktree> ",
            header="Select a tree-branch whose worktree to rebuild",
        )

    if target not in graph.parent_of:
        raise TreeError(
            f"{target} is not a repairable tree-branch — it has no tree-parent "
            f"(git tree repair won't touch a tree root).",
            code=5,
        )

    info = graph.branches.get(target)
    if not info or not info.worktree:
        stale = _prunable_worktree_path(target)
        if stale is not None:
            raise TreeError(
                f"{target}'s worktree at {stale} is gone, but git still has a stale "
                f"registration for it. `git tree repair` rebuilds a corrupted worktree in "
                f"place; it can't resurrect a deleted directory.\n"
                f"Recover with:\n"
                f"  git worktree prune\n"
                f"  git worktree add {stale} {target}",
                code=4,
            )
        raise TreeError(f"{target} has no worktree registered. Nothing to repair.", code=4)

    wt_path = info.worktree

    # Refuse if cwd is inside the target worktree
    try:
        cwd = Path.cwd().resolve()
        if cwd == wt_path.resolve() or cwd.is_relative_to(wt_path.resolve()):
            raise TreeError(
                f"Cannot repair {target}: your shell is inside its worktree ({wt_path}).\n"
                f"cd to a different directory first.",
                code=4,
            )
    except (OSError, ValueError):
        pass  # cwd resolution failed; proceed anyway

    # Dirty check: if git status works and shows changes, require --force
    force = getattr(args, "force", False)
    try:
        if info.is_dirty and not force:
            raise TreeError(
                f"{target} has uncommitted changes in {wt_path}.\n"
                f"Pass --force to repair anyway (uncommitted work will be lost).",
                code=4,
            )
    except subprocess.CalledProcessError:
        pass  # worktree too corrupted for git status; nothing to salvage

    # The branch's committed .gitmodules is the reliable signal: the (possibly corrupted)
    # worktree may be missing files, but the recreated one checks out the branch tip.
    has_submodules = git_ok("cat-file", "-e", f"{target}:.gitmodules")
    steps = ["Remove corrupted worktree", "Recreate worktree"]
    if has_submodules:
        steps.append("Initialize submodules")
    print(f"Repairing {target} at {wt_path}:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")
    print()
    if not _proceed(args, "Proceed?"):
        return

    _force_remove_worktree(wt_path, target)
    if not git_echo_ok("worktree", "add", str(wt_path), target):
        raise TreeError(f"Failed to recreate worktree at {wt_path}.")
    # Repair exists to make submodule state healthy, so a failed init is a failed repair — don't
    # claim "is healthy" over it.
    if not _init_submodules(wt_path):
        raise TreeError(
            f"Recreated {target}'s worktree at {wt_path}, but submodule init failed (see output "
            f"above). Fix the submodule issue, then re-run `git tree repair {target}`.",
            code=4,
        )
    print(f"\nRepaired {target} — worktree at {wt_path} is healthy.")


# [empty-patch handling]
#
# git rebase --onto can exit non-zero without producing merge conflicts.
# Two distinct cases:
#
# 1. No rebase started (REBASE_HEAD absent): git determined there was nothing
#    to replay and exited. The branch ref may already be updated. Return "ok".
#
# 2. Rebase started but stopped on an empty patch (REBASE_HEAD present, no
#    unmerged files): a commit's changes are already in the target. Common when
#    cascading through branches with no unique commits. Loop --skip until done
#    or a real conflict appears. Must loop — multi-commit branches can have
#    several empty patches in sequence.


def _skip_empty_commits(cwd: Path) -> str | None:
    """Loop --skip until rebase finishes or a real conflict appears. Returns None if
    a real conflict was hit (rebase left in progress for the user to resolve).

    Never aborts: a `--skip` that lands on a conflicting next commit is normal, so
    leave the rebase resumable rather than discarding it.
    """
    while _has_active_rebase(cwd):
        if git("ls-files", "--unmerged", cwd=cwd, check=False).strip():
            return None  # real conflict; leave rebase in progress
        if not git_echo_ok("rebase", "--skip", cwd=cwd):
            return None  # --skip surfaced a conflict / can't proceed; hand to user
    return "ok (skipped empty)"


def _rebase_onto(
    child: str,
    parent: str,
    fork_point: str,
    cwd: Path,
    auto_rerere: bool,
    stashed: bool,
) -> str:
    """Attempt rebase of child onto parent in its worktree. Returns status or exits on conflict."""
    head_before = git("rev-parse", "HEAD", cwd=cwd)
    result = git_echo("rebase", "--no-reapply-cherry-picks", "--onto", parent, fork_point, cwd=cwd)

    if result.returncode == 0:
        return "ok"

    if not _has_active_rebase(cwd):
        # Non-zero exit, no rebase left in progress. Confirm success positively:
        # the ref must have moved. Otherwise it's a real failure (bad ref,
        # pre-rebase hook reject, ...) — don't infer "ok" from stderr text.
        if git("rev-parse", "HEAD", cwd=cwd) != head_before:
            return "ok"
        # git_echo already reprinted git's stderr above.
        raise TreeError(f"rebase of {child} onto {parent} failed (see output above)")

    unmerged = git("ls-files", "--unmerged", cwd=cwd, check=False)
    if not unmerged.strip():
        status = _skip_empty_commits(cwd)
        if status is not None:
            return status
        _conflict_exit(child, parent, cwd, stashed)

    if not auto_rerere:
        _conflict_exit(child, parent, cwd, stashed)

    while True:
        git_echo("rerere", cwd=cwd)

        remaining = git("rerere", "remaining", cwd=cwd, check=False)
        if remaining.strip():
            _conflict_exit(child, parent, cwd, stashed)

        # git_echo swallows failures (check=False); a failed staging would otherwise loop
        # here forever, so treat it as an unresolvable conflict and stop.
        if not git_echo_ok("add", "-u", cwd=cwd):
            _conflict_exit(child, parent, cwd, stashed)

        continued = git_echo_ok("rebase", "--continue", cwd=cwd, env={"GIT_EDITOR": "true"})
        if continued:
            return "ok (rerere)"

        # --continue stopped again — new conflict or empty patch?
        new_unmerged = git("ls-files", "--unmerged", cwd=cwd, check=False)
        if not new_unmerged.strip():
            status = _skip_empty_commits(cwd)
            if status is not None:
                return "ok (rerere)"
            _conflict_exit(child, parent, cwd, stashed)


def _conflict_exit(child: str, parent: str, cwd: Path, stashed: bool) -> NoReturn:
    files = git_lines("diff", "--name-only", "--diff-filter=U", cwd=cwd)
    lines = [f"\nCONFLICT while rebasing {child} onto {parent}"]
    if files:
        lines.append("Conflicted files:")
        lines += [f"  {f}" for f in files]
    lines.append(f"Resolve the conflicts in {cwd} and `git add` them, then run: git tree continue")
    if stashed:
        lines.append(
            f"Note: dirty worktree was stashed — after continuing, run: cd {cwd} && git stash pop"
        )
    raise ConflictError(
        "\n".join(lines),
        branch=child,
        worktree=cwd,
        conflicted_files=files,
        remedy=["git", "tree", "continue"],
    )


def _require_worktrees(branches: list[str], graph: Graph) -> None:
    missing = [b for b in branches if not (graph.branches.get(b) and graph.branches[b].worktree)]
    if not missing:
        return
    lines = ["These branches need worktrees before this operation can proceed:"]
    for b in missing:
        lines.append(f"  {b}")
    lines.append("\nAdd worktrees with: git worktree add <path> <branch>")
    raise TreeError("\n".join(lines), code=4, branches=missing)


def _has_active_rebase(cwd: Path) -> bool:
    git_dir_path = _git_dir(cwd)
    return (git_dir_path / "rebase-merge").is_dir() or (git_dir_path / "rebase-apply").is_dir()


def _stash_push_if_created(cwd: Path) -> bool:
    """Stash tracked changes; return True iff a new stash entry was created.

    Detect via `refs/stash` advancing rather than parsing git's stdout, which is
    locale-dependent ("Saved working directory ..." is only English).
    """
    before = git("rev-parse", "--verify", "--quiet", "refs/stash", cwd=cwd, check=False)
    git_echo("stash", "push", cwd=cwd)
    after = git("rev-parse", "--verify", "--quiet", "refs/stash", cwd=cwd, check=False)
    return bool(after) and after != before


def _require_clean_state(branches: list[str], graph: Graph) -> None:
    problems = []
    for b in branches:
        info = graph.branches.get(b)
        if not info or not info.worktree:
            continue
        wt = info.worktree
        if _worktree_status(wt).conflicted:
            problems.append((b, wt, "unresolved conflicts"))
        elif _has_active_rebase(wt):
            problems.append((b, wt, "rebase in progress"))
    if not problems:
        return
    lines = ["These branches are not in a clean state:"]
    for b, wt, reason in problems:
        lines.append(f"  {b}  ({reason} — resolve in: {wt})")
    raise TreeError("\n".join(lines), code=4, branches=[b for b, _, _ in problems])


# ---------------------------------------------------------------------------
# Submodule helpers
# ---------------------------------------------------------------------------


def _submodule_paths(worktree: Path) -> list[str]:
    """Parse .gitmodules in a worktree, returning submodule paths that exist on disk."""
    gitmodules = worktree / ".gitmodules"
    if not gitmodules.exists():
        return []
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(str(gitmodules))
    paths = []
    for sec in cfg.sections():
        if cfg.has_option(sec, "path"):
            p = cfg.get(sec, "path")
            if (worktree / p).exists():
                paths.append(p)
    return paths


def _check_submodule_health(worktree: Path, submodule_path: str) -> bool:
    """True if a submodule's .git resolves to a valid git dir (has HEAD).

    Parses the .git file directly rather than shelling out, since the submodule
    may be in a corrupted state where git commands fail.
    """
    dot_git = worktree / submodule_path / ".git"
    if not dot_git.exists():
        return True  # Uninitialized — benign, not corrupted
    if dot_git.is_dir():
        return (dot_git / "HEAD").exists()
    # It's a file containing a gitdir: pointer
    try:
        content = dot_git.read_text().strip()
    except OSError:
        return False
    if not content.startswith("gitdir: "):
        return False
    target = Path(content.removeprefix("gitdir: "))
    if not target.is_absolute():
        target = (dot_git.parent / target).resolve()
    return target.is_dir() and (target / "HEAD").exists()


def _require_healthy_submodules(branches: list[str], graph: Graph) -> None:
    """Pre-flight: verify each worktree's submodules have valid .git state."""
    unhealthy: list[tuple[str, str]] = []
    for b in branches:
        info = graph.branches.get(b)
        if not info or not info.worktree:
            continue
        for sub_path in _submodule_paths(info.worktree):
            if not _check_submodule_health(info.worktree, sub_path):
                unhealthy.append((b, sub_path))
    if not unhealthy:
        return
    lines = ["These branches have corrupted submodule state:"]
    for b, sub_path in unhealthy:
        lines.append(f"  {b}  (submodule: {sub_path})")
    lines.append("\nFix with: git tree repair <branch>")
    raise TreeError("\n".join(lines), code=4)


def _init_submodules(worktree: Path) -> bool:
    """Run `git submodule update --init --recursive` if .gitmodules exists. Returns True on
    success (or when there are no submodules), False if the init failed."""
    if not (worktree / ".gitmodules").exists():
        return True
    return git_echo_ok("submodule", "update", "--init", "--recursive", cwd=worktree)


def _init_submodules_or_warn(path: str) -> None:
    """Init submodules after creating a worktree; warn (don't fail) if it didn't complete — the
    branch/worktree itself was created, so this is a follow-up the user can finish by hand."""
    if not _init_submodules(Path(path)):
        print(
            f"Warning: submodule init did not complete (see output above); run "
            f"`git submodule update --init --recursive` in {path}.",
            file=sys.stderr,
        )


def _force_remove_worktree(path: Path, branch: str) -> None:
    """Remove a worktree by any means necessary.

    Stage 1: git worktree remove --force
    Stage 2: shutil.rmtree + git worktree prune
    Stage 3: verify gone from git worktree list
    """
    if git_echo_ok("worktree", "remove", "--force", str(path)):
        return
    if path.exists():
        shutil.rmtree(path)
    git_echo("worktree", "prune")
    # Verify it's no longer registered
    porcelain = git("worktree", "list", "--porcelain")
    if any(line == f"branch refs/heads/{branch}" for line in porcelain.splitlines()):
        raise TreeError(
            f"Could not fully deregister worktree for {branch}. "
            f"Manual cleanup may be needed in .git/worktrees/.",
            code=4,
        )


@dataclass(frozen=True)
class RebaseResult:
    note: str  # how the rebase completed, for display: "ok", "ok (rerere)", ...
    pop_conflicted: bool = False


def _rebase_branch(
    branch: str,
    onto: str,
    fork_point: str,
    info: BranchInfo,
    *,
    auto_rerere: bool,
) -> RebaseResult:
    """Rebase `branch` onto `onto` in its worktree, stashing/popping dirty changes
    and recording the new fork point. Raises (via _rebase_onto) on a real conflict,
    leaving the rebase in progress. A pop conflict is non-fatal (the branch ref is
    already rebased); it's reported via `pop_conflicted` and the worktree is left
    for the user."""
    cwd = info.worktree
    assert cwd is not None  # callers guarantee a worktree via _require_worktrees
    stashed = info.is_dirty and _stash_push_if_created(cwd)
    note = _rebase_onto(branch, onto, fork_point, cwd, auto_rerere, stashed)
    # Rebase succeeded; record the fork before the pop (which only touches the
    # working tree). `rev-parse(onto)` is stable here — rebasing `branch` never
    # moves `onto`.
    _set_fork_commit(branch, git("rev-parse", onto))
    pop_conflicted = stashed and not git_echo_ok("stash", "pop", cwd=cwd)
    return RebaseResult(note, pop_conflicted)


def _propagate_descendants(
    branch: str,
    graph: Graph,
    *,
    auto_rerere: bool = True,
) -> list[tuple[str, str]]:
    descendants = graph.downstream_from(branch)
    results: list[tuple[str, str]] = []

    if descendants:
        print("Results:")
    for child in descendants:
        parent = graph.parent_of[child]
        info = graph.branches[child]
        fork_point = _get_fork_commit(child, parent, info)
        r = _rebase_branch(child, parent, fork_point, info, auto_rerere=auto_rerere)
        text = "rebased (stash pop conflict - resolve manually)" if r.pop_conflicted else r.note
        # Stream each result as it lands: a mid-cascade conflict raises before this returns,
        # so streaming is what makes the already-rebased branches visible.
        print(f"  {child}: {text}")
        results.append((child, text))

    return results


def cmd_propagate(args: argparse.Namespace) -> None:
    branch = getattr(args, "branch", None) or current_branch()
    graph = discover()

    descendants = graph.downstream_from(branch)
    if not descendants:
        print("No descendants to propagate to.")
        return

    _require_worktrees(descendants, graph)
    _require_healthy_submodules(descendants, graph)
    _require_clean_state(descendants, graph)

    print(f"Propagating from {branch}:")
    for line in _subtree_lines(graph, branch, show_counts=True):
        print(line)
    print()

    if getattr(args, "dry_run", False) or not _proceed(args, "Proceed?"):
        return
    auto_rerere = not getattr(args, "no_auto_rerere", False)

    print()
    _propagate_descendants(branch, graph, auto_rerere=auto_rerere)


def cmd_rebase(args: argparse.Namespace) -> None:
    branch = current_branch()
    target: str = args.target
    graph = discover()

    old_parent = graph.parent_of.get(branch) or _get_tree_parent(branch)
    if not old_parent:
        raise TreeError(f"{branch} has no tree-parent-branch configured.", code=5)

    if not git_ok("rev-parse", "--verify", old_parent):
        raise TreeError(f"Old parent {old_parent} does not exist.")

    if not git_ok("rev-parse", "--verify", target):
        raise TreeError(f"Rebase target {target} does not exist.")

    if target == branch:
        raise TreeError(f"Cannot rebase {branch} onto itself.")
    if _would_cycle(branch, target):
        raise TreeError(
            f"Cannot rebase {branch} onto {target}: {target} descends from {branch} "
            f"in the tree (would create a cycle)."
        )

    fork_point = _get_fork_commit(branch, old_parent, graph.branches.get(branch))
    commit_count = len(git_lines("rev-list", f"{fork_point}..{branch}"))

    descendants = graph.downstream_from(branch)

    siblings = [b for b, p in graph.parent_of.items() if p == old_parent and b != branch]

    info = graph.branches.get(branch)
    if not info or not info.worktree:
        raise TreeError(
            f"{branch} needs a worktree. Add one with: git worktree add <path> {branch}",
            code=4,
        )
    _require_healthy_submodules([branch], graph)
    _require_clean_state([branch], graph)
    if descendants:
        _require_worktrees(descendants, graph)
        _require_healthy_submodules(descendants, graph)
        _require_clean_state(descendants, graph)

    print(f"Rebasing onto {target}:")
    print(f"  {branch}  [{commit_count} commits]  (old parent: {old_parent})")

    if descendants:
        print()
        print("Will propagate to:")
        for line in _subtree_lines(graph, branch):
            print(f"  {line}")

    if siblings:
        print()
        print(f"Warning: these branches also have {old_parent} as parent (will NOT be updated):")
        for s in siblings:
            print(f"  {s}")

    print()
    if args.dry_run or not _proceed(args, "Proceed?"):
        return

    auto_rerere = not getattr(args, "no_auto_rerere", False)

    # Reparent + carry the remote BEFORE the rebase so a conflict leaves config pointing at
    # the intended new parent, letting `git tree continue` resume onto `target` uniformly.
    # (A rare non-conflict rebase failure then leaves config at target with branch unmoved —
    # a benign, idempotent retry.) Reparenting onto an out-of-tree target re-roots branch's
    # tree; both roots come from the pre-rebase in-memory graph, which the config write
    # doesn't perturb: root_of(branch) is the old root, root_of(target) the new.
    git("config", f"branch.{branch}.tree-parent-branch", target)
    _carry_remote_to_root(root_of(graph, branch), root_of(graph, target))
    r = _rebase_branch(branch, target, fork_point, info, auto_rerere=auto_rerere)
    if r.pop_conflicted:
        print(
            f"Warning: could not pop worktree stash — run: cd {info.worktree} && git stash pop",
            file=sys.stderr,
        )
    print(f"Rebased {branch} onto {target}")

    if descendants:
        print()
        print("Cascading to descendants...")
        _propagate_descendants(branch, graph, auto_rerere=auto_rerere)


def _branch_containing_cwd(branches: list[str], graph: Graph) -> str | None:
    """The single branch in `branches` whose worktree contains the current directory, else None."""
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return None
    matches = []
    for b in branches:
        wt = graph.branches[b].worktree
        if wt and cwd.is_relative_to(wt.resolve()):  # is_relative_to is true on equality too
            matches.append(b)
    return matches[0] if len(matches) == 1 else None


def cmd_continue(args: argparse.Namespace) -> None:
    """Resume a cascade stopped by a conflict: finish the in-progress rebase (with the editor
    disabled, so no hang), record the new fork point, then propagate to the branch's
    descendants. Replaces the raw `git rebase --continue` + `git tree propagate` dance."""
    graph = discover()
    stuck = [
        b
        for b, info in graph.branches.items()
        if info.worktree and _has_active_rebase(info.worktree)
    ]
    if not stuck:
        raise TreeError("No rebase in progress in any tree worktree — nothing to continue.", code=4)
    if len(stuck) > 1:
        # Two independent trees can each be mid-rebase. Disambiguate by the worktree the caller
        # is in; a bare scan can't target one, so require the caller to be inside it.
        branch = _branch_containing_cwd(stuck, graph)
        if branch is None:
            raise TreeError(
                f"Multiple worktrees are mid-rebase ({', '.join(sorted(stuck))}); cd into the "
                f"one you want to resume, then run `git tree continue`.",
                code=4,
            )
    else:
        branch = stuck[0]
    wt = graph.branches[branch].worktree
    assert wt is not None
    onto = graph.parent_of.get(branch)
    if onto is None:
        raise TreeError(
            f"{branch} is mid-rebase but has no tree-parent; finish it with `git rebase "
            f"--continue` in {wt}.",
            code=4,
        )

    if git("ls-files", "--unmerged", cwd=wt, check=False).strip():
        raise TreeError(
            f"{branch} still has unresolved conflicts in {wt}. Resolve them and `git add` the "
            f"files, then run `git tree continue`.",
            code=4,
            kind="unresolved_conflicts",
            branches=[branch],
        )

    git_echo("rebase", "--continue", cwd=wt, env={"GIT_EDITOR": "true"})
    if _has_active_rebase(wt):
        # `--continue` stopped again: either a later commit conflicts, or it resolved to an
        # empty patch that must be skipped (mirrors the cascade's own handling). `stashed=False`:
        # a stash from the original run isn't detectable here, and its first conflict already
        # told the user about it.
        if git("ls-files", "--unmerged", cwd=wt, check=False).strip():
            _conflict_exit(branch, onto, wt, stashed=False)
        if _skip_empty_commits(wt) is None:
            _conflict_exit(branch, onto, wt, stashed=False)

    _set_fork_commit(branch, git("rev-parse", onto))
    print(f"Continued {branch} onto {onto}.")
    # Resume the WHOLE cascade, not just `branch`'s subtree. A conflict aborts the flat
    # topological walk, so branches queued after `branch` that aren't its descendants (siblings,
    # cousins) were skipped. The original cascade's scope isn't recorded, so re-propagate from
    # the tree root: already-current branches replay as empty and no-op, only genuinely-stale
    # ones rebase. Re-discover first so `branch`'s just-updated tip/fork is reflected.
    graph = discover()
    root = root_of(graph, branch)
    descendants = graph.downstream_from(root)
    # Same preflight as cmd_propagate: the resume now reaches branches outside the original
    # cascade's scope, so guard them the same way (worktrees before submodule/clean checks, since
    # `git status` crashes on a corrupted submodule) rather than asserting deep in _rebase_branch.
    _require_worktrees(descendants, graph)
    _require_healthy_submodules(descendants, graph)
    _require_clean_state(descendants, graph)
    _propagate_descendants(root, graph, auto_rerere=not getattr(args, "no_auto_rerere", False))


def _resolve_split_point(after: str, old_fork: str | None) -> str:
    """Resolve a `--after` commit-ish to a full hash and require it to sit in the
    splittable range (`old_fork..HEAD`, or all of HEAD's history for a root). Raises
    TreeError if it doesn't resolve or falls outside that range."""
    resolved = git("rev-parse", "--verify", "--quiet", f"{after}^{{commit}}", check=False)
    if not resolved:
        raise TreeError(f"--after: '{after}' is not a valid commit.")
    if not git_ok("merge-base", "--is-ancestor", resolved, "HEAD"):
        raise TreeError(f"--after: {after} is not an ancestor of HEAD.")
    if old_fork is not None and git_ok("merge-base", "--is-ancestor", resolved, old_fork):
        raise TreeError(
            f"--after: {after} is at or below this branch's fork point; "
            f"pick a commit unique to this branch."
        )
    return resolved


def _worktree_choice(args: argparse.Namespace, name: str) -> str:
    """Worktree path for the new branch, or "" for none: `--worktree PATH` / `--no-worktree`,
    else the interactive `[path / N]` prompt (where 'n' means none)."""
    wt = getattr(args, "worktree", None)
    if wt:
        return wt
    if getattr(args, "no_worktree", False):
        return ""
    _require_input(args, "worktree choice", "--worktree PATH or --no-worktree")
    reply = _prompt(f"Create worktree for {name}? [path / N]: ") or ""
    return "" if reply.lower() == "n" else reply


def _split_child(
    args: argparse.Namespace, branch: str, old_fork: str | None, commit_hash: str
) -> None:
    """`git tree split --child`: keep `branch` (and its worktree) for the commits up to
    `commit_hash`, peeling the later commits onto a new child branch. `branch` rewinds to
    `commit_hash`; the new branch is created at the old tip first so no commit is lost, and
    `branch`'s existing tree-children re-point onto it (forks left as-is — the new branch
    inherits the old tip's full history). `branch`'s own parent/fork are untouched."""
    new_name = getattr(args, "name", None)
    if not new_name:
        _require_input(args, "new branch name", "--name")
        new_name = _prompt("New child branch name: ")
    if not new_name:
        raise SystemExit(1)

    # The rewind resets the worktree, so refuse tracked changes (untracked survive it).
    st = _worktree_status(Path(git("rev-parse", "--show-toplevel")))
    if st.staged or st.modified or st.conflicted:
        raise TreeError(
            f"{branch} has uncommitted changes; --child rewinds the worktree to the split "
            f"commit. Commit or stash them first.",
            code=4,
        )

    old_head = git("rev-parse", "HEAD")
    children = [b for b in all_branch_names() if _get_tree_parent(b) == branch]

    # Best-effort: rewinding past what was pushed will diverge from the remote.
    upstream = git("rev-parse", "--verify", "--quiet", f"{branch}@{{upstream}}", check=False)
    if upstream and not git_ok("merge-base", "--is-ancestor", upstream, commit_hash):
        print(
            f"Warning: {branch} is pushed; rewinding past its upstream. The remote will "
            f"diverge until your next `git tree push`.",
            file=sys.stderr,
        )

    # Create the new child at the old tip BEFORE rewinding, so no commit is lost.
    if not git_echo_ok("branch", new_name, old_head):
        raise TreeError(f"Could not create branch '{new_name}' (see output above).")
    if not git_echo_ok("reset", "--hard", commit_hash):
        raise TreeError(
            f"Failed to rewind {branch} to {commit_hash} (see output above); "
            f"'{new_name}' was created at the old tip."
        )

    # New child hangs off `branch` (now at the split); `branch` keeps its own parent/fork.
    _register_child(new_name, branch, fork=commit_hash)
    # `branch`'s children were tracking its old tip, which `new_name` now carries (with the
    # full old history). Re-point them so a later propagate lands each child where it would
    # have before the split; their fork commits stay valid because `new_name` holds them.
    for c in children:
        git("config", f"branch.{c}.tree-parent-branch", new_name)

    worktree_path = _worktree_choice(args, new_name)
    if worktree_path:
        if git_echo_ok("worktree", "add", worktree_path, new_name):
            print(f"Created worktree at {worktree_path}")
        else:
            print(
                f"Warning: could not create worktree at {worktree_path} "
                f"(the split itself succeeded; add one later with "
                f"`git worktree add <path> {new_name}`).",
                file=sys.stderr,
            )

    kept_range = f"{old_fork}..{commit_hash}" if old_fork is not None else commit_hash
    kept = git_lines("log", "--oneline", kept_range)
    moved = git_lines("log", "--oneline", f"{commit_hash}..{old_head}")
    print("\nSplit complete:")
    print(f"  {branch} ({len(kept)} commits) → keeps the work up to the split")
    print(f"  {new_name} ({len(moved)} commits) → new child branch")
    if children:
        print(f"  reparented onto {new_name}: {', '.join(children)}")


def cmd_split(args: argparse.Namespace) -> None:
    branch = current_branch()
    parent = _get_tree_parent(branch)

    # A child splits the commits above its fork from `parent`; that fork is inherited by
    # the new parent, which takes the child's old position. A root has no fork, so its
    # splittable range is its full history and the new parent it yields is itself a root.
    old_fork: str | None
    if parent:
        old_fork = _get_fork_commit(branch, parent)
        commits = git_lines("log", "--oneline", "--reverse", f"{old_fork}..HEAD")
    else:
        old_fork = None
        commits = git_lines("log", "--oneline", "--reverse", "HEAD")

    if len(commits) < 2:
        raise TreeError("Need at least 2 commits to split.")

    child_mode = getattr(args, "child", False)

    after = getattr(args, "after", None)
    if after:
        commit_hash = _resolve_split_point(after, old_fork)
    else:
        _require_input(args, "split commit", "--after COMMIT")
        header = (
            f"Select the last commit to keep on {branch}"
            if child_mode
            else "Select the last commit for the new parent branch"
        )
        commit_hash = _select_one(commits, prompt="Split after> ", header=header).split()[0]

    if child_mode:
        _split_child(args, branch, old_fork, commit_hash)
        return

    parent_name = getattr(args, "name", None)
    if not parent_name:
        _require_input(args, "new branch name", "--name")
        parent_name = _prompt("New parent branch name: ")
    if not parent_name:
        raise SystemExit(1)

    # Create the branch before prompting for a worktree, so a bad name (already taken or
    # invalid) fails fast with git's own message instead of a traceback from a bare git().
    if not git_echo_ok("branch", parent_name, commit_hash):
        raise TreeError(f"Could not create branch '{parent_name}' (see output above).")

    worktree_path = _worktree_choice(args, parent_name)

    git("config", f"branch.{branch}.tree-parent-branch", parent_name)
    _set_fork_commit(branch, git("rev-parse", commit_hash))
    if old_fork is not None:
        # Child split: the new parent inherits the child's former parent and fork point.
        git("config", f"branch.{parent_name}.tree-parent-branch", parent)
        _set_fork_commit(parent_name, old_fork)
    else:
        # Root split: parent_name becomes the tree's new root, so the remote anchor that
        # lived on the old root (`branch`) moves onto it. branch keeps neither parent nor
        # fork — it is now the new root's child, recorded above.
        _carry_remote_to_root(branch, parent_name)

    if worktree_path:
        # The split (branch + config) is already applied; a worktree-add failure
        # must not abort and leave the user unsure whether the split happened.
        if git_echo_ok("worktree", "add", worktree_path, parent_name):
            print(f"Created worktree at {worktree_path}")
        else:
            print(
                f"Warning: could not create worktree at {worktree_path} "
                f"(the split itself succeeded; add one later with "
                f"`git worktree add <path> {parent_name}`).",
                file=sys.stderr,
            )

    split_range = f"{old_fork}..{commit_hash}" if old_fork is not None else commit_hash
    split_commits = git_lines("log", "--oneline", split_range)
    remaining = git_lines("log", "--oneline", f"{commit_hash}..HEAD")
    print("\nSplit complete:")
    print(f"  {parent_name} ({len(split_commits)} commits) → new parent branch")
    print(f"  {branch} ({len(remaining)} commits) → now child of {parent_name}")


def cmd_push(args: argparse.Namespace) -> dict | None:
    branch = current_branch()
    graph = discover()

    # Hard-error (unlike cmd_log's benign exit) so a stray `git tree push` on a plain
    # branch like `main` can never force-push it to the branch's own `branch.remote`.
    if branch not in graph.parent_of and branch not in graph.children_of:
        raise TreeError("Not on a tree-branch.", code=5)

    descendants = graph.downstream_from(branch)
    push_set = [branch] + descendants
    _require_worktrees([b for b in push_set if b in graph.branches], graph)

    # One remote per tree, defined on the root; every branch pushes there.
    root, root_remote = _root_remote(graph, branch)
    if not root_remote:
        raise TreeError(
            f"root tree-branch '{root}' has no remote configured "
            f"(set it with `git config branch.{root}.remote <remote>`)"
        )

    # Note: intentionally do NOT fetch here. `--force-with-lease` (no explicit
    # expected ref) compares the remote against the remote-tracking ref; fetching
    # first would advance that ref to a teammate's commit and let the force-push
    # silently clobber it. The un-fetched ref reflects our last known state and is
    # exactly what makes the lease reject a clobber.

    stale: list[str] = []
    ahead: dict[str, int] = {}
    new_roots: set[str] = set()

    for b in push_set:
        parent = graph.parent_of.get(b)
        if parent and b != branch:
            merge_base = git("merge-base", parent, b)
            parent_tip = git("rev-parse", parent)
            if merge_base != parent_tip:
                stale.append(b)
                continue

        remote_ref = f"{root_remote}/{b}"
        if git_ok("rev-parse", "--verify", remote_ref):
            ahead[b] = len(git_lines("rev-list", f"{remote_ref}..{b}"))
        elif parent:
            base = git("merge-base", parent, b)
            ahead[b] = len(git_lines("rev-list", f"{base}..{b}"))
        else:
            # Never-pushed root: no remote ref and no parent to count against, so an
            # "ahead" number is meaningless (merge-base(b, b) is b -> a bogus 0). It's new.
            new_roots.add(b)

    pushable = [b for b in push_set if b not in stale]

    # A branch whose ancestor in this run is stale can't be pushed — its base
    # wouldn't be on the remote. Propagate that through descendants (push_set is
    # topological, parents before children) for an accurate preview.
    blocked = set(stale)
    for b in pushable:
        if graph.parent_of.get(b) in blocked:
            blocked.add(b)

    if all(b in blocked for b in pushable):
        print("Nothing to push.")
        return

    print(f"Pushing to {root_remote} (--force-with-lease):")
    for b in push_set:
        if b in stale:
            print(f"  {b}  (stale - run propagate first)")
        elif b in blocked:
            print(f"  {b}  (skipped - ancestor not pushed)")
        elif b in new_roots:
            print(f"  {b}  (new)")
        else:
            print(f"  {b}  [{ahead.get(b, 0)} ahead]")
    print()

    if args.dry_run or not _proceed(args, "Proceed?"):
        return

    results: list[tuple[str, str]] = []
    failed: list[str] = []
    lease_rejected = False
    for b in pushable:
        # Skip if an ancestor in this run is stale or its push failed. Re-add b so
        # the block cascades to its own descendants later in the loop.
        if graph.parent_of.get(b) in blocked:
            results.append((b, "skipped (ancestor not pushed)"))
            blocked.add(b)
            continue

        res = git_echo("push", "--force-with-lease", "-u", root_remote, b)
        if res.returncode == 0:
            results.append((b, "ok"))
        else:
            blocked.add(b)
            failed.append(b)
            if "stale info" in (res.stderr or ""):
                lease_rejected = True  # the lease caught a remote that moved under us
            results.append((b, "FAILED"))

    print()
    _print_results(results)

    if failed:
        # A push that fails must not exit 0 (latent bug). A lease rejection means the remote
        # advanced — fetch + propagate, then retry — which is distinct from a transport/hook
        # failure, so an agent gets a specific `kind`.
        hint = (
            " (lease rejected: the remote moved — fetch, `git tree propagate`, then retry)"
            if lease_rejected
            else ""
        )
        raise TreeError(
            f"push failed for: {', '.join(failed)}{hint}",
            code=1,
            kind="lease_rejected" if lease_rejected else None,
            branches=failed,
        )

    # Surface what was NOT pushed. This is the one place a bare {ok:true} under-informs an
    # agent: stale/blocked branches are silently left behind and the skip classification isn't
    # cleanly re-derivable from a forest snapshot. (Human mode ignores the return.)
    return {
        "skipped": [{"branch": b, "reason": "stale"} for b in stale]
        + [
            {"branch": b, "reason": "ancestor_not_pushed"}
            for b, status in results
            if status.startswith("skipped")
        ]
    }


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ZSH_COMPLETION = """\
#compdef git-tree

_git-tree() {
    local -a subcmds
    subcmds=(
        'propagate:Propagate changes to all descendants'
        'rebase:Rebase current branch + descendants onto new base'
        'continue:Resume a cascade after resolving a conflict'
        'branch:Create or adopt a child branch with a worktree'
        'attach:Attach current branch to tree'
        'detach:Remove a branch from tree'
        'remove:Remove a subtree’s worktrees, keep the branch refs'
        'repair:Nuke and recreate a corrupted worktree'
        'split:Split current branch into parent + child'
        'push:Push current branch + descendants'
        'log:Show git log graph for all tree-branches'
        'completions:Emit shell completion script'
        'manpage:Emit a man page (roff); --install writes it to the man path'
    )

    if (( CURRENT == 2 )); then
        _describe 'subcommand' subcmds
        return
    fi

    case $words[2] in
        propagate)
            _arguments \
                '--dry-run[Show what would be done]' \
                '--no-auto-rerere[Disable auto-continue via rerere]' \
                '(-y --yes)'{-y,--yes}'[Skip the confirmation prompt]' \
                ':branch:__git_heads'
            ;;
        push)
            _arguments \
                '--dry-run[Show what would be done]' \
                '(-y --yes)'{-y,--yes}'[Skip the confirmation prompt]'
            ;;
        rebase)
            _arguments \
                '--dry-run[Show what would be done]' \
                '--no-auto-rerere[Disable auto-continue via rerere]' \
                '(-y --yes)'{-y,--yes}'[Skip the confirmation prompt]' \
                ':target:__git_heads'
            ;;
        branch)
            _arguments ':path:_directories' ':name:'
            ;;
        split)
            _arguments \
                '--after[Commit to split after]:commit:__git_heads' \
                '--name[New branch name]:name:' \
                '--child[Keep current branch for early commits; new branch takes the rest]' \
                '--worktree[Create the new branch worktree]:path:_directories' \
                '--no-worktree[Do not create a worktree for the new branch]'
            ;;
        attach)
            _arguments ':branch:__git_heads'
            ;;
        detach)
            _arguments \
                '(-y --yes)'{-y,--yes}'[Skip the confirmation prompt]' \
                ':branch:__git_heads'
            ;;
        remove)
            _arguments \
                '--dry-run[Show what would be done]' \
                '(-y --yes)'{-y,--yes}'[Skip the confirmation prompt]' \
                ':branch:__git_heads'
            ;;
        repair)
            _arguments \
                '--force[Proceed even if worktree has uncommitted changes]' \
                '(-y --yes)'{-y,--yes}'[Skip the confirmation prompt]' \
                ':branch:__git_heads'
            ;;
        completions)
            _arguments ':shell:(zsh bash)'
            ;;
        manpage)
            _arguments \
                '--install[Write the man page to the man path]' \
                '--dir[Install directory]:dir:_directories'
            ;;
    esac
}

_git-tree "$@"
"""

_BASH_COMPLETION = """\
_git_tree() {
    local cur prev subcmds
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    subcmds="propagate rebase continue branch attach detach remove"
    subcmds="$subcmds repair split push log completions manpage"

    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$subcmds" -- "$cur"))
        return
    fi

    case "${COMP_WORDS[1]}" in
        propagate)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--dry-run --no-auto-rerere -y --yes" -- "$cur"))
            else
                local branches=$(git for-each-ref --format='%(refname:short)' refs/heads/)
                COMPREPLY=($(compgen -W "$branches" -- "$cur"))
            fi
            ;;
        push)
            COMPREPLY=($(compgen -W "--dry-run -y --yes" -- "$cur"))
            ;;
        rebase)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--dry-run --no-auto-rerere -y --yes" -- "$cur"))
            else
                local branches=$(git for-each-ref --format='%(refname:short)' refs/heads/)
                COMPREPLY=($(compgen -W "$branches" -- "$cur"))
            fi
            ;;
        branch)
            COMPREPLY=($(compgen -d -- "$cur"))
            ;;
        split)
            if [[ "$cur" == -* ]]; then
                local opts="--after --name --child --worktree --no-worktree"
                COMPREPLY=($(compgen -W "$opts" -- "$cur"))
            fi
            ;;
        attach)
            local branches=$(git for-each-ref --format='%(refname:short)' refs/heads/)
            COMPREPLY=($(compgen -W "$branches" -- "$cur"))
            ;;
        detach)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "-y --yes" -- "$cur"))
            else
                local branches=$(git for-each-ref --format='%(refname:short)' refs/heads/)
                COMPREPLY=($(compgen -W "$branches" -- "$cur"))
            fi
            ;;
        remove)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--dry-run -y --yes" -- "$cur"))
            else
                local branches=$(git for-each-ref --format='%(refname:short)' refs/heads/)
                COMPREPLY=($(compgen -W "$branches" -- "$cur"))
            fi
            ;;
        repair)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--force -y --yes" -- "$cur"))
            else
                local branches=$(git for-each-ref --format='%(refname:short)' refs/heads/)
                COMPREPLY=($(compgen -W "$branches" -- "$cur"))
            fi
            ;;
        completions)
            COMPREPLY=($(compgen -W "zsh bash" -- "$cur"))
            ;;
        manpage)
            COMPREPLY=($(compgen -W "--install --dir" -- "$cur"))
            ;;
    esac
}

complete -F _git_tree git-tree
"""


def cmd_completions(args: argparse.Namespace) -> None:
    if args.shell == "zsh":
        print(_ZSH_COMPLETION)
    else:
        print(_BASH_COMPLETION)


def _render_manpage(parser: argparse.ArgumentParser) -> str:
    """Render a roff man page whose body is the argparse help, verbatim.

    Single source of truth: the same parser feeds `-h`, `--help`, and this page. Width is
    pinned (argparse otherwise wraps usage/options to the generating terminal's width, so the
    output would vary by environment). Installing this page is what makes `git tree --help`
    work: git routes `--help` to `man git-tree`.
    """
    prev_columns = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "80"
    try:
        help_text = parser.format_help()
    finally:
        if prev_columns is None:
            del os.environ["COLUMNS"]
        else:
            os.environ["COLUMNS"] = prev_columns

    # Escape for roff, order matters: backslash first; then literal grave/apostrophe (groff
    # renders bare `/' as typographic quotes, mandoc does not); then neutralize any line a
    # roff parser would read as a request (leading `.` or `'`) so it prints literally.
    lines = []
    for raw in help_text.split("\n"):
        line = raw.replace("\\", "\\e").replace("`", "\\(ga").replace("'", "\\(aq")
        if line[:1] in (".", "'"):
            line = "\\&" + line
        lines.append(line)
    body = "\n".join(lines)

    return (
        ".TH GIT-TREE 1\n"
        ".SH NAME\n"
        "git-tree \\- Cascading rebase tool for branch dependency chains\n"
        ".SH DESCRIPTION\n"
        ".nf\n"
        f"{body}"
        ".fi\n"
    )


def cmd_manpage(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    roff = _render_manpage(parser)
    if not getattr(args, "install", False):
        sys.stdout.write(roff)
        return
    man_dir = Path(args.dir).expanduser() if args.dir else Path.home() / ".local/share/man/man1"
    man_dir.mkdir(parents=True, exist_ok=True)
    dest = man_dir / "git-tree.1"
    dest.write_text(roff)
    print(f"Installed man page to {dest}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


_EPILOG = """\
tree state (git config, no external files):
  branch.<name>.tree-parent-branch   the branch it stacks on
  branch.<name>.tree-fork-commit     where it forks from that parent
  branch.<root>.remote               the tree's single remote (on the root)

FOR AGENTS:
  git tree --json    agent mode: exactly one JSON envelope on stdout, all diagnostics
                     (git echoes, warnings) on stderr; implies --no-input, disables color.
                     The forest query keeps its roots/cycles/orphans/branches keys (each
                     branch also has rebase_in_progress). Mutations return a bare {ok:true}
                     — re-query the forest for post-op state.
  -y, --yes          skip confirmation on propagate/rebase/push/remove/repair/detach;
                     under --json a needed confirm returns kind=confirmation_required
                     (re-run with -y; never auto-confirmed)
  git tree continue  resume a cascade after resolving a conflict: finish the rebase (editor
                     disabled), record the new fork point, propagate to descendants
  --no-input         never prompt; error instead of asking for a value
  --dry-run          preview propagate/rebase/push/remove without mutating
  --version          print git-tree <version>
  exit codes         3 resumable conflict, 4 precondition/state, 5 not-a-tree-branch;
                     error.kind is one of usage/conflict/precondition/not_a_tree_branch/error
                     plus input_required/confirmation_required/lease_rejected/unresolved_conflicts
  full contract      see CLAUDE.md in the git-tree source repo
"""


_KIND_BY_CODE = {2: "usage", 3: "conflict", 4: "precondition", 5: "not_a_tree_branch"}


def _envelope(args: argparse.Namespace, data: dict | None = None) -> dict:
    env = {"command": getattr(args, "command", None) or "tree", "ok": True}
    if data:
        env.update(data)  # flat merge: e.g. cmd_tree's forest keys become siblings
    return env


def _error_envelope(args: argparse.Namespace, err: TreeError) -> dict:
    env = _envelope(args)
    env["ok"] = False
    error: dict = {
        "kind": err.kind or _KIND_BY_CODE.get(err.code, "error"),
        "code": err.code,
        "message": err.message,
    }
    if err.branches:
        error["branches"] = err.branches
    if isinstance(err, ConflictError):
        error["branch"] = err.branch
        error["worktree"] = str(err.worktree)
        error["conflicted_files"] = err.conflicted_files
    if err.remedy:
        error["remedy"] = err.remedy
    env["error"] = error
    return env


def _render_error(args: argparse.Namespace, err: TreeError, out) -> NoReturn:
    # The human message is already on stderr (TreeError.__init__). In agent mode, also write
    # the structured envelope to the real stdout so the agent gets exactly one JSON object.
    if getattr(args, "json", False):
        print(json.dumps(_error_envelope(args, err), indent=2), file=out)
    raise SystemExit(err.code)


def main(argv: list[str] | None = None) -> None:  # explicit argv for tests
    parser = argparse.ArgumentParser(
        prog="git-tree",
        description=__doc__,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="With no subcommand, show all trees instead of just the current one",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Agent mode: emit one machine-readable JSON object on stdout (implies --no-input)",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="Never prompt; error (exit 4) if a value would be asked for interactively",
    )
    parser.add_argument("--version", action="version", version=f"git-tree {_version()}")
    # --no-input is accepted both before the subcommand (top-level, above) and after it
    # (via this shared parent). SUPPRESS on the parent copy means an absent flag leaves the
    # top-level value intact instead of clobbering it with the subparser's default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-input", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    propagate_p = sub.add_parser(
        "propagate", help="Propagate changes to all descendants", parents=[common]
    )
    propagate_p.add_argument(
        "branch", nargs="?", help="Branch to propagate from (default: current)"
    )
    propagate_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    propagate_p.add_argument(
        "--no-auto-rerere", action="store_true", help="Disable auto-continue via rerere"
    )
    propagate_p.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    rebase_p = sub.add_parser(
        "rebase", help="Rebase current branch + descendants onto new base", parents=[common]
    )
    rebase_p.add_argument("target", help="Branch or ref to rebase onto")
    rebase_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    rebase_p.add_argument(
        "--no-auto-rerere", action="store_true", help="Disable auto-continue via rerere"
    )
    rebase_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    continue_p = sub.add_parser(
        "continue", help="Resume a cascade after resolving a conflict", parents=[common]
    )
    continue_p.add_argument(
        "--no-auto-rerere", action="store_true", help="Disable auto-continue via rerere"
    )

    branch_p = sub.add_parser(
        "branch", help="Create or adopt a child branch with a worktree", parents=[common]
    )
    branch_p.add_argument("path", help="Worktree path for the branch")
    branch_p.add_argument("name", help="Branch name (new, or an existing branch to adopt)")
    branch_p.add_argument(
        "--no-submodule-init",
        action="store_true",
        help="Skip automatic `git submodule update --init --recursive` after creating the worktree",
    )

    attach_p = sub.add_parser("attach", help="Attach current branch to tree", parents=[common])
    attach_p.add_argument("parent", nargs="?", help="Parent branch (fzf if omitted)")

    detach_p = sub.add_parser("detach", help="Remove a branch from tree", parents=[common])
    detach_p.add_argument("branch", nargs="?", help="Branch to detach (default: current)")
    detach_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    remove_p = sub.add_parser(
        "remove",
        help="Remove a subtree's worktrees and unregister its branches (keeps refs)",
        parents=[common],
    )
    remove_p.add_argument("branch", nargs="?", help="Branch to remove (default: pick via fzf)")
    remove_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    remove_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    repair_p = sub.add_parser(
        "repair",
        help="Nuke and recreate a corrupted worktree (preserves branch ref and tree config)",
        parents=[common],
    )
    repair_p.add_argument("branch", nargs="?", help="Branch to repair (default: pick via fzf)")
    repair_p.add_argument(
        "--force", action="store_true", help="Proceed even if worktree has uncommitted changes"
    )
    repair_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    split_p = sub.add_parser(
        "split", help="Split current branch into parent + child", parents=[common]
    )
    split_p.add_argument("--after", metavar="COMMIT", help="Commit to split after (fzf if omitted)")
    split_p.add_argument("--name", metavar="BRANCH", help="New branch name (prompt if omitted)")
    split_p.add_argument(
        "--child",
        action="store_true",
        help="Keep the current branch for the early commits; new branch takes the rest",
    )
    split_wt = split_p.add_mutually_exclusive_group()
    split_wt.add_argument(
        "--worktree", metavar="PATH", help="Create the new branch's worktree at PATH"
    )
    split_wt.add_argument(
        "--no-worktree", action="store_true", help="Don't create a worktree for the new branch"
    )

    push_p = sub.add_parser("push", help="Push current branch + descendants", parents=[common])
    push_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    push_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    sub.add_parser("log", help="Show git log graph for all tree-branches", parents=[common])

    completions_p = sub.add_parser(
        "completions", help="Emit shell completion script", parents=[common]
    )
    completions_p.add_argument("shell", choices=["zsh", "bash"], help="Shell type")

    manpage_p = sub.add_parser(
        "manpage",
        help="Emit a man page (roff); --install writes it to the man path",
        parents=[common],
    )
    manpage_p.add_argument(
        "--install",
        action="store_true",
        help="Write the man page under the man path instead of printing to stdout",
    )
    manpage_p.add_argument(
        "--dir", metavar="DIR", help="Directory to install into (default: ~/.local/share/man/man1)"
    )

    args, unknown = parser.parse_known_args(argv)
    if unknown and getattr(args, "command", None) != "log":
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    if getattr(args, "command", None) == "log":
        args.extra = unknown

    commands = {
        None: cmd_tree,
        "propagate": cmd_propagate,
        "rebase": cmd_rebase,
        "continue": cmd_continue,
        "branch": cmd_branch,
        "attach": cmd_attach,
        "detach": cmd_detach,
        "remove": cmd_remove,
        "repair": cmd_repair,
        "split": cmd_split,
        "push": cmd_push,
        "log": cmd_log,
    }

    # Single output edge. In agent mode all inline human/`git_echo` output is redirected to
    # stderr (a stdlib context manager, restored on exit) so stdout carries exactly one JSON
    # object, written after the handler returns. Errors render here too — one place.
    agent = getattr(args, "json", False)
    real_stdout = sys.stdout
    try:
        if args.command == "manpage":
            # Emits roff (or installs) to the real stdout; --json is not meaningful here.
            cmd_manpage(args, parser)
        elif args.command == "completions":
            cmd_completions(args)  # shell script to real stdout; --json not meaningful
        elif args.command == "log" and agent:
            raise TreeError(
                "`git tree log` has no JSON form; query state with `git tree --json`.", code=2
            )
        else:
            handler = commands.get(args.command, cmd_tree)
            with contextlib.redirect_stdout(sys.stderr) if agent else contextlib.nullcontext():
                # Only cmd_tree returns envelope data (the forest); others return None.
                data = handler(args)
            if agent:
                print(json.dumps(_envelope(args, data), indent=2), file=real_stdout)
    except subprocess.CalledProcessError as e:
        # A bare git() (check=True) failed somewhere unexpected. Surface git's own command
        # and stderr as a clean error instead of dumping a CalledProcessError traceback.
        cmd = " ".join(e.cmd) if isinstance(e.cmd, (list, tuple)) else str(e.cmd)
        stderr = (e.stderr or "").strip()
        _render_error(
            args,
            TreeError(
                f"git command failed (exit {e.returncode}): {cmd}"
                + (f"\n{stderr}" if stderr else "")
            ),
            real_stdout,
        )
    except TreeError as e:
        _render_error(args, e, real_stdout)


if __name__ == "__main__":
    main()
