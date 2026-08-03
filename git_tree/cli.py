"""git-tree: Cascading rebase tool for branch dependency chains."""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import NoReturn

from git_tree._cmd_attach import cmd_attach
from git_tree._cmd_branch import cmd_branch
from git_tree._cmd_detach import cmd_detach
from git_tree._cmd_log import cmd_log
from git_tree._cmd_skills import cmd_skills
from git_tree._cmd_tree import cmd_tree
from git_tree._display import _subtree_lines, format_tree
from git_tree._engine import (
    _advance_branch,
    _auto_rerere,
    _propagate_descendants,
    _rebase_branch,
    _resume_cmd,
)
from git_tree._errors import ConflictError, ErrorKind, TreeError
from git_tree._git import (
    _active_rebase_branch,
    _carry_remote_to_root,
    _force_remove_worktree,
    _get_tree_parent,
    _has_active_rebase,
    _has_active_rebase_safe,
    _init_submodules,
    _is_git_tree_rebase,
    _pending_sequencer_op,
    _register_child,
    _set_fork_commit,
    _unset_tree_config,
    _worktree_status,
    _would_cycle,
    all_branch_names,
    current_branch,
    git,
    git_echo,
    git_echo_ok,
    git_lines,
    git_ok,
)
from git_tree._graph import (
    _get_fork_commit,
    _root_remote,
    discover,
    root_of,
)
from git_tree._guards import (
    _mid_rebase_branches,
    _remove_blocking_dirt,
    _require_clean_state,
    _require_healthy_submodules,
    _require_ready,
    _require_worktrees,
)
from git_tree._prompt import (
    _proceed,
    _prompt,
    _require_input,
    _select_one,
)
from git_tree._render import _render_completions, _render_manpage, _set_completer


def _version() -> str:
    try:
        return metadata.version("git-tree")
    except metadata.PackageNotFoundError:
        return "0+unknown"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_remove(args: argparse.Namespace) -> None:
    """Tear down a subtree's worktrees and unregister its branches from the tree.

    Removes worktree directories and unsets tree config; it never deletes a branch ref, so no
    committed work can be lost. Uncommitted work IS at risk (worktrees are force-removed), so by
    default it refuses if any worktree or submodule is dirty; `--force` overrides that (and
    destroys the uncommitted work, including inside submodules and git-ignored files).
    """
    graph = discover()
    try:
        cur: str | None = current_branch()
    except TreeError:
        cur = None

    target = args.branch
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

    force = args.force

    # cwd guard: force-removal deletes the directory outright (bypassing git's "can't delete the
    # tree you're standing in" protection), and a following `git worktree prune` from a deleted
    # cwd errors. Refuse if the shell is inside any worktree being removed.
    try:
        cwd = Path.cwd().resolve()
        inside = [
            b
            for b in subtree
            if (info := graph.branches.get(b))
            and info.worktree
            and cwd.is_relative_to(info.worktree.resolve())
        ]
        if inside:
            raise TreeError(
                f"Your shell is inside a worktree being removed ({', '.join(inside)}). "
                f"cd to a different directory first.",
                code=4,
                branches=inside,
            )
    except (OSError, ValueError):
        pass  # cwd resolution failed; proceed

    # A stopped rebase is usually dirty, so the gate below caught this by accident rather than on
    # purpose. It stops at a clean point often enough to matter (an --exec failure, git-tree's own
    # empty-patch stop), and then the worktree holds the only reference to every conflict already
    # resolved in that rebase: the branch ref hasn't moved, so the work lives on the detached HEAD
    # and in the worktree's own HEAD reflog, both of which go with the directory.
    mid_rebase = [
        b
        for b in subtree
        if (info := graph.branches.get(b))
        and info.worktree
        and _has_active_rebase_safe(info.worktree)
    ]
    if mid_rebase and not force:
        raise TreeError(
            "Refusing to remove: a rebase is in progress in:\n"
            + "\n".join(f"  {b}  ({graph.branches[b].worktree})" for b in mid_rebase)
            + "\n\nFinish it (`git tree propagate <branch>`) or `git rebase --abort` there, or "
            "re-run with --force to discard it. Nothing was removed.",
            code=4,
            branches=mid_rebase,
        )

    # Safety gate (all-or-nothing): worktrees are force-removed, so uncommitted work (in the
    # worktree OR any submodule at any depth) is at risk. Refuse unless --force. Branch refs are
    # kept, so committed work is never at risk.
    dirty = [
        b
        for b in subtree
        if (info := graph.branches.get(b))
        and info.worktree
        and _remove_blocking_dirt(info.worktree)
    ]
    if dirty and not force:
        lines = [
            "Refusing to remove: these worktrees have uncommitted changes "
            "(possibly inside a submodule):"
        ]
        lines += [f"  {b}  ({graph.branches[b].worktree})" for b in dirty]
        lines.append(
            "\nCommit, stash, or discard them, or re-run with --force to remove anyway. "
            "Nothing was removed."
        )
        raise TreeError("\n".join(lines), code=4, branches=dirty)

    print(f"Removing worktrees and unregistering {target} + its subtree (branch refs kept):")
    print(format_tree(graph, root=target))
    # Said on every path, not just --force: `git status` never reports ignored files, so they are
    # deleted without ever having been counted as dirt. `.env` files and venvs live here.
    print("\nThis deletes each worktree directory, git-ignored files included.")
    if dirty:  # implies force
        print("\nWarning: --force will destroy uncommitted changes (and any git-ignored files) in:")
        for b in dirty:
            print(f"  {b}  ({graph.branches[b].worktree})")
    if mid_rebase:  # implies force
        print("\nWarning: --force will discard the rebase in progress in:")
        for b in mid_rebase:
            print(f"  {b}  ({graph.branches[b].worktree})")
    print()
    if args.dry_run:
        return
    if not _proceed(args, "Remove these worktrees and detach the branches?"):
        return

    # Re-scan once, all-or-nothing, before deleting anything: closes the check->delete window
    # that plain `git worktree remove` used to guard (a worktree could go dirty during the
    # prompt). --force already opted out of the gate.
    if not force:
        late = [
            b
            for b in subtree
            if (info := graph.branches.get(b))
            and info.worktree
            and (_remove_blocking_dirt(info.worktree) or _has_active_rebase_safe(info.worktree))
        ]
        if late:
            raise TreeError(
                "These worktrees became dirty after confirmation; nothing was removed:\n"
                + "\n".join(f"  {b}  ({graph.branches[b].worktree})" for b in late),
                code=4,
                branches=late,
            )

    # Children-first. `_force_remove_worktree` handles submodule worktrees git refuses to remove
    # and raises TreeError on genuine failure (so removal stops rather than report false success).
    removed_worktrees = 0
    for b in reversed(subtree):
        info = graph.branches.get(b)
        if info and info.worktree:
            _force_remove_worktree(info.worktree, b)
            removed_worktrees += 1
        _unset_tree_config(b)

    print(
        f"\nDetached {len(subtree)} branch(es) from the tree; "
        f"removed {removed_worktrees} worktree(s). Branch refs kept."
    )


def _prunable_worktree_path(branch: str) -> Path | None:
    """Path of a stale (prunable) worktree registration for `branch`, if any.

    Its directory was deleted (rm -rf'd) without `git worktree prune`, so `discover()`
    drops it and the branch looks worktree-less. `cmd_rebuild` uses this to point the user
    at recovery rather than a bare "nothing to rebuild"."""
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


def cmd_rebuild(args: argparse.Namespace) -> None:
    """Rebuild a corrupted worktree from the branch tip, preserving branch ref and tree config."""
    graph = discover()

    target = args.branch
    if target is None:
        _require_input(args, "branch to rebuild", "the branch argument")
        candidates = sorted(
            b for b in graph.parent_of if (info := graph.branches.get(b)) and info.worktree
        )
        if not candidates:
            raise TreeError("No tree-branch worktrees available to rebuild.")
        target = _select_one(
            candidates,
            prompt="Rebuild worktree> ",
            header="Select a tree-branch whose worktree to rebuild",
        )

    if target not in graph.parent_of:
        raise TreeError(
            f"git tree rebuild only acts on tree-branches; {target} has no tree-parent "
            f"(rebuild won't touch a tree root).",
            code=5,
        )

    info = graph.branches.get(target)
    if not info or not info.worktree:
        stale = _prunable_worktree_path(target)
        if stale is not None:
            raise TreeError(
                f"{target}'s worktree at {stale} is gone, but git still has a stale "
                f"registration for it. `git tree rebuild` recreates a corrupted worktree in "
                f"place; it can't resurrect a deleted directory.\n"
                f"Recover with:\n"
                f"  git worktree prune\n"
                f"  git worktree add {stale} {target}",
                code=4,
            )
        raise TreeError(f"{target} has no worktree registered. Nothing to rebuild.", code=4)

    wt_path = info.worktree

    # Refuse if cwd is inside the target worktree
    try:
        cwd = Path.cwd().resolve()
        wt = wt_path.resolve()
        if cwd.is_relative_to(wt):
            raise TreeError(
                f"Cannot rebuild {target}: your shell is inside its worktree ({wt_path}).\n"
                f"cd to a different directory first.",
                code=4,
            )
    except (OSError, ValueError):
        pass  # cwd resolution failed; proceed anyway

    force = args.force
    if not force and _has_active_rebase_safe(wt_path):
        raise TreeError(
            f"{target} has a rebase in progress in {wt_path}. Rebuilding discards it, along with "
            f"every conflict already resolved in it (the branch ref has not moved, so that work "
            f"exists only in this worktree).\nFinish it (`git tree propagate {target}`) or "
            f"`git rebase --abort` there, or re-run with --force.",
            code=4,
        )

    # Same gate `remove` uses, not a bare `git status`: that misses submodule dirt whenever
    # .gitmodules carries `ignore = all`, and misses a populated-but-uninitialized submodule
    # directory entirely. Both are real content, and rebuild deletes the worktree.
    #
    # Read the worktree's own state first, with submodules excluded so a corrupted one cannot
    # make this fail. It answers two questions. If it cannot run at all, nothing here is
    # inspectable, so refuse rather than delete blind. If the fuller gate below then fails, that
    # failure is the submodule corruption rebuild exists to repair, so fall back to this answer
    # instead of refusing: demanding --force there would demand it for rebuild's whole purpose.
    own_dirt: bool | None = None
    try:
        own_dirt = _worktree_status(
            wt_path, ignore_submodules="all", untracked_files="normal"
        ).dirty
    except subprocess.CalledProcessError as err:
        if not force:
            raise TreeError(
                f"Could not read {wt_path} at all (git status failed there), so git-tree cannot "
                f"tell whether it holds uncommitted work.\nRescue anything you need from that "
                f"directory, then re-run with --force.",
                code=4,
            ) from err
    try:
        blocked = _remove_blocking_dirt(wt_path)
    except subprocess.CalledProcessError:
        print(
            f"Warning: could not inspect submodules under {wt_path}; "
            f"checking the worktree itself only.",
            file=sys.stderr,
        )
        blocked = bool(own_dirt)
    if blocked and not force:
        raise TreeError(
            f"{target} has uncommitted changes in {wt_path} (possibly inside a submodule).\n"
            f"Pass --force to rebuild anyway (uncommitted work will be lost).",
            code=4,
        )

    # The branch's committed .gitmodules is the reliable signal: the (possibly corrupted)
    # worktree may be missing files, but the recreated one checks out the branch tip.
    has_submodules = git_ok("cat-file", "-e", f"{target}:.gitmodules")
    steps = ["Remove corrupted worktree", "Recreate worktree"]
    if has_submodules:
        steps.append("Initialize submodules")
    print(f"Rebuilding {target} at {wt_path}:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")
    print()
    if not _proceed(args, "Proceed?"):
        return

    _force_remove_worktree(wt_path, target)
    if not git_echo_ok("worktree", "add", str(wt_path), target):
        raise TreeError(f"Failed to recreate worktree at {wt_path}.")
    # Rebuild exists to make submodule state healthy, so a failed init is a failed rebuild; don't
    # claim "is healthy" over it.
    if not _init_submodules(wt_path):
        raise TreeError(
            f"Recreated {target}'s worktree at {wt_path}, but submodule init failed (see output "
            f"above). Fix the submodule issue, then re-run `git tree rebuild {target}`.",
            code=4,
        )
    print(f"\nRebuilt {target}: worktree at {wt_path} is healthy.")


def cmd_propagate(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    graph = discover()

    descendants = graph.downstream_from(branch)
    resume_cmd = _resume_cmd(branch)

    # `propagate` is also the universal resume: if a conflict stopped an earlier cascade, the
    # user resolves + `git add` and re-runs this command, which finishes the interrupted rebase
    # before continuing. The stuck branch is either `branch` itself (a `git tree rebase` left it
    # mid-rebase) or one of its descendants.
    info = graph.branches.get(branch)
    named_mid = bool(info and info.worktree and _has_active_rebase(info.worktree))
    descendant_mid = any(
        (di := graph.branches.get(d)) and di.worktree and _has_active_rebase(di.worktree)
        for d in descendants
    )
    is_resume = named_mid or descendant_mid

    # A leaf left mid-rebase by `git tree rebase` still needs finishing, so don't take the
    # no-descendants shortcut when `branch` itself is the stuck one.
    if not descendants and not named_mid:
        print("No descendants to propagate to.")
        return

    _require_ready(descendants, graph, resume_cmd)

    print(f"Propagating from {branch}:")
    for line in _subtree_lines(graph, branch, show_counts=True):
        print(line)
    print()

    if args.dry_run:
        return
    # A resume continues an already-confirmed cascade — don't re-prompt (this also keeps the
    # agent `remedy` runnable without -y). Only a fresh propagate asks.
    if not is_resume and not _proceed(args, "Proceed?"):
        return
    auto_rerere = _auto_rerere(args)

    print()
    if named_mid:
        assert info is not None
        parent = graph.parent_of.get(branch)
        if parent is None:
            raise TreeError(
                f"{branch} is mid-rebase but has no tree-parent; finish it with "
                f"`git rebase --continue` in its worktree.",
                code=4,
            )
        _advance_branch(
            branch,
            parent,
            info,
            _get_fork_commit(branch, parent, info),
            auto_rerere=auto_rerere,
            resume_cmd=resume_cmd,
        )
        graph = discover()  # branch's tip/fork moved; re-read before cascading
    _propagate_descendants(branch, graph, auto_rerere=auto_rerere, resume_cmd=resume_cmd)


def cmd_rebase(args: argparse.Namespace) -> None:
    if args.branch is not None:
        branch = args.branch
        # Naming a branch that doesn't exist would otherwise surface as "no tree-parent-branch
        # configured", which reads as a tree problem rather than the typo it is.
        if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            raise TreeError(f"No such branch: {branch}", code=4)
    else:
        try:
            branch = current_branch()
        except TreeError:
            # A detached HEAD here usually means a prior `git tree rebase` conflicted and left this
            # worktree mid-rebase. Rebase is never the resume verb, so point at the propagate that
            # is (naming the stuck branch). `git_ok` guards the non-repo case so we don't mask it.
            cwd = Path.cwd()
            if git_ok("rev-parse", "--git-dir", cwd=cwd) and _has_active_rebase(cwd):
                stuck = _active_rebase_branch(cwd)
                hint = f"git tree propagate {stuck}" if stuck else "git tree propagate <branch>"
                raise TreeError(
                    "This worktree is mid-rebase from an earlier `git tree rebase`. Resolve the "
                    f"conflicts and `git add`, then resume with: {hint}",
                    code=4,
                ) from None
            raise
    target: str = args.target
    graph = discover()
    resume_cmd = _resume_cmd(branch)

    old_parent = graph.parent_of.get(branch) or _get_tree_parent(branch)
    if not old_parent:
        raise TreeError(f"{branch} has no tree-parent-branch configured.", code=5)

    if not git_ok("rev-parse", "--verify", old_parent):
        raise TreeError(f"Old parent {old_parent} does not exist.")

    # The target becomes branch's tree-parent, and tree-parents are always local branches
    # (discover() flags any non-branch parent as orphaned). Reject a tag/commit/remote-tracking
    # ref up front rather than writing an edge the next discover() would break.
    if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{target}"):
        raise TreeError(
            f"Rebase target '{target}' is not a local branch; git-tree can only reparent onto "
            f"a local branch. Create it first, or pick an existing branch.",
            code=4,
        )

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
    # Naming a branch reaches a state the current-branch form cannot: a mid-rebase worktree has a
    # detached HEAD, so `current_branch()` fails there and the hint above fires instead. Reached by
    # name, the reparent below would commit and the rebase would then drive the in-progress rebase
    # to completion, `--skip`ping past the very commits being replayed. `_require_clean_state` does
    # not cover this: it admits a resolved git-tree mid-rebase as a resume point, by design.
    if _has_active_rebase(info.worktree):
        # Same predicate the cascade uses, so the advice below cannot send the user to a
        # `propagate` that then refuses this very worktree as foreign.
        ours = _is_git_tree_rebase(info.worktree, old_parent)
        fix = (
            f"resolve the conflicts and `git add` them, then run: {' '.join(resume_cmd)}"
            if ours
            else "finish it or `git rebase --abort` first"
        )
        raise TreeError(
            f"{branch} is mid-rebase in {info.worktree}; rebase is not the resume verb. {fix}",
            code=4,
        )

    # Rewriting `branch` invalidates any rebase in progress below it: its base is about to stop
    # being an ancestor of its parent, so the cascade would reach it and refuse it as a rebase
    # git-tree did not start, with no way back to this state. `_require_ready` below admits such a
    # rebase as a resume point, which is right for `propagate` and wrong here, so refuse while it
    # can still be finished, and before the reparent write.
    if stopped := _mid_rebase_branches(descendants, graph):
        lines = [f"Rebasing {branch} would invalidate a rebase already in progress below it:"]
        for d, wt in stopped:
            fix = (
                f"finish it with: git tree propagate {d}"
                if _is_git_tree_rebase(wt, graph.parent_of.get(d))
                else "not started by git-tree: finish it or `git rebase --abort` there"
            )
            lines.append(f"  {d}  (in: {wt}) {fix}")
        lines.append(f"\nThen re-run: git tree rebase {target} {branch}")
        raise TreeError("\n".join(lines), code=4, branches=[d for d, _ in stopped])

    _require_healthy_submodules([branch], graph)
    _require_clean_state([branch], graph, resume_cmd)
    if descendants:
        _require_ready(descendants, graph, resume_cmd)

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

    auto_rerere = _auto_rerere(args)

    # Reparent + carry the remote BEFORE the rebase so a conflict leaves config pointing at
    # the intended new parent. That makes a conflict resumable the same way any propagate conflict
    # is: `git tree propagate <branch>` (the reparent is already committed, so propagate finishes
    # branch's own rebase onto target, then cascades to descendants).
    # Reparenting onto an out-of-tree target re-roots branch's tree; both roots come from the
    # pre-rebase in-memory graph, which the config write doesn't perturb.
    git("config", f"branch.{branch}.tree-parent-branch", target)
    _carry_remote_to_root(root_of(graph, branch), root_of(graph, target))
    r = _rebase_branch(
        branch, target, fork_point, info, auto_rerere=auto_rerere, resume_cmd=resume_cmd
    )
    if r.unpopped_stash:
        print(
            f"Warning: could not pop worktree stash; your changes are still in it. Restore them "
            f"with: cd {info.worktree} && git stash apply {r.unpopped_stash}",
            file=sys.stderr,
        )
    print(f"Rebased {branch} onto {target}")

    if descendants:
        print()
        print("Cascading to descendants...")
        _propagate_descendants(branch, graph, auto_rerere=auto_rerere, resume_cmd=resume_cmd)


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
    wt = args.worktree
    if wt:
        return wt
    if args.no_worktree:
        return ""
    _require_input(args, "worktree choice", "--worktree PATH or --no-worktree")
    reply = _prompt(f"Create worktree for {name}? [path / N]: ") or ""
    return "" if reply.lower() == "n" else reply


def _add_split_worktree(worktree_path: str, name: str) -> None:
    """Create `name`'s worktree at `worktree_path`, warning (not failing) if it can't be made.

    The split's branch and config writes are already applied by the time this runs, so a
    worktree-add failure must not abort and leave the user unsure whether the split happened.
    No-op when `worktree_path` is empty (the user declined a worktree).
    """
    if not worktree_path:
        return
    if git_echo_ok("worktree", "add", worktree_path, name):
        print(f"Created worktree at {worktree_path}")
    else:
        print(
            f"Warning: could not create worktree at {worktree_path} "
            f"(the split itself succeeded; add one later with "
            f"`git worktree add <path> {name}`).",
            file=sys.stderr,
        )


def _split_child(
    args: argparse.Namespace, branch: str, old_fork: str | None, commit_hash: str
) -> None:
    """`git tree split --child`: keep `branch` (and its worktree) for the commits up to
    `commit_hash`, peeling the later commits onto a new child branch. `branch` rewinds to
    `commit_hash`; the new branch is created at the old tip first so no commit is lost, and
    `branch`'s existing tree-children re-point onto it (forks left as-is — the new branch
    inherits the old tip's full history). `branch`'s own parent/fork are untouched."""
    new_name = args.name
    if not new_name:
        _require_input(args, "new branch name", "--name")
        new_name = _prompt("New child branch name: ")
    if not new_name:
        raise SystemExit(1)

    # The rewind resets the worktree, so refuse tracked changes (untracked survive it).
    top = Path(git("rev-parse", "--show-toplevel"))
    st = _worktree_status(top)
    if st.staged or st.modified or st.conflicted:
        raise TreeError(
            f"{branch} has uncommitted changes; --child rewinds the worktree to the split "
            f"commit. Commit or stash them first.",
            code=4,
        )
    # A merge or cherry-pick whose resolutions are staged looks clean to the check above once
    # they are staged, and `reset --hard` would drop its state silently. Unlike a mid-rebase,
    # which detaches HEAD and so never reaches here, these keep HEAD attached.
    if (op := _pending_sequencer_op(top)) is not None:
        raise TreeError(
            f"{branch} has a {op} in progress in {top}; --child rewinds the worktree and would "
            f"discard it. Finish or abort it first.",
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

    # Confirm before the destructive rewind: reset --hard rewrites branch's worktree to the split
    # commit (its later commits are preserved on new_name, created just below). This is the one
    # split path that rewinds a worktree, so it confirms like the other destructive commands.
    moved_count = len(git_lines("log", "--oneline", f"{commit_hash}..{old_head}"))
    if not _proceed(
        args,
        f"Rewind {branch} to the split commit (git reset --hard) and move its {moved_count} "
        f"later commit(s) onto {new_name}?",
    ):
        return

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
    _add_split_worktree(worktree_path, new_name)

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

    child_mode = args.child

    after = args.after
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

    parent_name = args.name
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

    _add_split_worktree(worktree_path, parent_name)

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
    print("Results:")
    for name, status in results:
        print(f"  {name}: {status}")

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
            kind=ErrorKind.LEASE_REJECTED if lease_rejected else None,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def cmd_completions(args: argparse.Namespace) -> None:
    print(_render_completions(_build_parser(), args.shell))


def cmd_manpage(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    roff = _render_manpage(parser)
    if not args.install:
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
                     — re-query for post-op state (exceptions: push's `skipped`, and
                     `skills` whose listing is a query and carries per-destination state).
  -y, --yes          skip confirmation on propagate/rebase/push/remove/rebuild/detach;
                     under --json a needed confirm returns kind=confirmation_required
                     (re-run with -y; never auto-confirmed)
  resume a conflict  resolve the files, `git add`, then re-run `git tree propagate <branch>`
                     (the branch you were operating on); it finishes the interrupted rebase
                     (editor disabled) and continues the cascade — no `git rebase --continue`
  --no-input         never prompt; error instead of asking for a value
  --dry-run          preview propagate/rebase/push/remove without mutating
  --version          print git-tree <version>
  exit codes         3 resumable conflict, 4 precondition/state, 5 not-a-tree-branch;
                     error.kind is one of usage/conflict/precondition/not_a_tree_branch/error
                     plus input_required/confirmation_required/lease_rejected/unresolved_conflicts
  full contract      see AGENTS.md in the git-tree source repo
"""


_KIND_BY_CODE = {
    2: ErrorKind.USAGE,
    3: ErrorKind.CONFLICT,
    4: ErrorKind.PRECONDITION,
    5: ErrorKind.NOT_A_TREE_BRANCH,
}


def _envelope(args: argparse.Namespace, data: dict | None = None) -> dict:
    env = {"command": args.command or "tree", "ok": True}
    if data:
        env.update(data)  # flat merge: e.g. cmd_tree's forest keys become siblings
    return env


def _error_envelope(args: argparse.Namespace, err: TreeError) -> dict:
    env = _envelope(args)
    env["ok"] = False
    error: dict = {
        "kind": err.kind or _KIND_BY_CODE.get(err.code, ErrorKind.ERROR),
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
    if args.json:
        print(json.dumps(_error_envelope(args, err), indent=2), file=out)
    raise SystemExit(err.code)


def _build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser. Sole source of truth for the command surface: `main`
    dispatches on it (via each subparser's set_defaults(func=...)), and `_render_manpage`, `-h`,
    and `_render_completions` (both shells) all derive from it. A subcommand added here is wired
    for dispatch, help, the man page, and completions by that one `add_parser` block; a value arg
    that should complete branches or paths is tagged via `_set_completer(..., "git_heads")` (or
    `"directories"`)."""
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
    # Handler dispatch lives on the parser: the no-subcommand default is cmd_tree, and each
    # dispatchable subparser overrides it via set_defaults(func=...) below. main() calls args.func.
    parser.set_defaults(func=cmd_tree)
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
    propagate_p.set_defaults(func=cmd_propagate)
    _set_completer(
        propagate_p.add_argument(
            "branch", nargs="?", help="Branch to propagate from (default: current)"
        ),
        "git_heads",
    )
    propagate_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    propagate_p.add_argument(
        "--no-auto-rerere", action="store_true", help="Disable auto-continue via rerere"
    )
    propagate_p.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    rebase_p = sub.add_parser(
        "rebase", help="Rebase a branch + descendants onto new base", parents=[common]
    )
    rebase_p.set_defaults(func=cmd_rebase)
    _set_completer(
        rebase_p.add_argument("target", help="Branch or ref to rebase onto"), "git_heads"
    )
    _set_completer(
        rebase_p.add_argument("branch", nargs="?", help="Branch to rebase (default: current)"),
        "git_heads",
    )
    rebase_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    rebase_p.add_argument(
        "--no-auto-rerere", action="store_true", help="Disable auto-continue via rerere"
    )
    rebase_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    branch_p = sub.add_parser(
        "branch", help="Create or adopt a child branch with a worktree", parents=[common]
    )
    branch_p.set_defaults(func=cmd_branch)
    _set_completer(
        branch_p.add_argument("path", help="Worktree path for the branch"), "directories"
    )
    branch_p.add_argument("name", help="Branch name (new, or an existing branch to adopt)")
    branch_p.add_argument(
        "--no-submodule-init",
        action="store_true",
        help="Skip automatic `git submodule update --init --recursive` after creating the worktree",
    )

    attach_p = sub.add_parser("attach", help="Attach current branch to tree", parents=[common])
    attach_p.set_defaults(func=cmd_attach)
    _set_completer(
        attach_p.add_argument("parent", nargs="?", help="Parent branch (fzf if omitted)"),
        "git_heads",
    )

    detach_p = sub.add_parser("detach", help="Remove a branch from tree", parents=[common])
    detach_p.set_defaults(func=cmd_detach)
    _set_completer(
        detach_p.add_argument("branch", nargs="?", help="Branch to detach (default: current)"),
        "git_heads",
    )
    detach_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    remove_p = sub.add_parser(
        "remove",
        help="Remove a subtree's worktrees and unregister its branches (keeps refs)",
        parents=[common],
    )
    remove_p.set_defaults(func=cmd_remove)
    _set_completer(
        remove_p.add_argument("branch", nargs="?", help="Branch to remove (default: pick via fzf)"),
        "git_heads",
    )
    remove_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    remove_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    remove_p.add_argument(
        "--force",
        action="store_true",
        help="Remove even if a worktree or its submodules have uncommitted changes",
    )

    rebuild_p = sub.add_parser(
        "rebuild",
        help="Rebuild a corrupted worktree from the branch tip (keeps branch ref and tree config)",
        parents=[common],
    )
    rebuild_p.set_defaults(func=cmd_rebuild)
    _set_completer(
        rebuild_p.add_argument(
            "branch", nargs="?", help="Branch to rebuild (default: pick via fzf)"
        ),
        "git_heads",
    )
    rebuild_p.add_argument(
        "--force", action="store_true", help="Proceed even if worktree has uncommitted changes"
    )
    rebuild_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    split_p = sub.add_parser(
        "split", help="Split current branch into parent + child", parents=[common]
    )
    split_p.set_defaults(func=cmd_split)
    _set_completer(
        split_p.add_argument(
            "--after", metavar="COMMIT", help="Commit to split after (fzf if omitted)"
        ),
        "git_heads",
    )
    split_p.add_argument("--name", metavar="BRANCH", help="New branch name (prompt if omitted)")
    split_p.add_argument(
        "--child",
        action="store_true",
        help="Keep the current branch for the early commits; new branch takes the rest",
    )
    split_wt = split_p.add_mutually_exclusive_group()
    _set_completer(
        split_wt.add_argument(
            "--worktree", metavar="PATH", help="Create the new branch's worktree at PATH"
        ),
        "directories",
    )
    split_wt.add_argument(
        "--no-worktree", action="store_true", help="Don't create a worktree for the new branch"
    )
    split_p.add_argument(
        "-y", "--yes", action="store_true", help="Skip the --child rewind confirmation prompt"
    )

    push_p = sub.add_parser("push", help="Push current branch + descendants", parents=[common])
    push_p.set_defaults(func=cmd_push)
    push_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    push_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    log_p = sub.add_parser("log", help="Show git log graph for all tree-branches", parents=[common])
    log_p.set_defaults(func=cmd_log)

    skills_p = sub.add_parser(
        "skills",
        help="List the bundled agent skills; --install links them into your agent harnesses",
        parents=[common],
    )
    skills_p.set_defaults(func=cmd_skills)
    skills_p.add_argument(
        "--install",
        action="store_true",
        help="Install the skills into ~/.claude/skills and ~/.agents/skills",
    )
    _set_completer(
        skills_p.add_argument(
            "--dir",
            metavar="DIR",
            help="Use DIR instead of the per-harness directories (listing and install alike)",
        ),
        "directories",
    )

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
    _set_completer(
        manpage_p.add_argument(
            "--dir",
            metavar="DIR",
            help="Directory to install into (default: ~/.local/share/man/man1)",
        ),
        "directories",
    )
    return parser


def main(argv: list[str] | None = None) -> None:  # explicit argv for tests
    parser = _build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown and args.command != "log":
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    if args.command == "log":
        args.extra = unknown

    # Single output edge. In agent mode all inline human/`git_echo` output is redirected to
    # stderr (a stdlib context manager, restored on exit) so stdout carries exactly one JSON
    # object, written after the handler returns. Errors render here too, in one place.
    agent = args.json
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
            # manpage/completions are handled above (pre-dispatch); every other subparser sets
            # func, and the top-level default covers the no-subcommand case.
            handler = args.func
            with contextlib.redirect_stdout(sys.stderr) if agent else contextlib.nullcontext():
                # Queries return envelope data (cmd_tree's forest, cmd_skills' listing); mutations
                # return None and stay bare, except cmd_push's non-re-derivable skip set.
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
