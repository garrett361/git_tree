"""The `split` command: divide a branch into a parent and a child at a chosen commit."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from git_tree._errors import TreeError
from git_tree._git import (
    _carry_remote_to_root,
    _get_tree_parent,
    _init_submodules_or_warn,
    _pending_sequencer_op,
    _register_child,
    _set_fork_commit,
    _worktree_status,
    all_branch_names,
    current_branch,
    git,
    git_echo_ok,
    git_lines,
    git_ok,
)
from git_tree._graph import _get_fork_commit
from git_tree._guards import _require_initialized_submodules
from git_tree._prompt import _proceed, _prompt, _require_input, _select_one
from git_tree._registry import subcommand
from git_tree._render import _set_completer

if TYPE_CHECKING:
    import argparse


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


def _add_split_worktree(worktree_path: str, name: str, *, init_submodules: bool = True) -> None:
    """Create `name`'s worktree at `worktree_path`, warning (not failing) if it can't be made.

    The split's branch and config writes are already applied by the time this runs, so a
    worktree-add failure must not abort and leave the user unsure whether the split happened.
    No-op when `worktree_path` is empty (the user declined a worktree).

    `git worktree add` leaves submodules unpopulated, and a later `git tree split --child` run
    from here would `reset --hard` into them, so initialize as `branch` does. A failed init only
    warns, for the same reason a failed worktree-add does.
    """
    if not worktree_path:
        return
    if git_echo_ok("worktree", "add", worktree_path, name):
        print(f"Created worktree at {worktree_path}")
        if init_submodules:
            _init_submodules_or_warn(worktree_path)
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
    # Before anything, including the name prompt: a submodule that the split commit records but
    # this worktree cannot open makes the rewind below fail partway. This also has to precede the
    # `git status` read, which itself dies on a corrupted submodule.
    top = Path(git("rev-parse", "--show-toplevel"))
    _require_initialized_submodules(top, commit_hash, branch)

    new_name = args.name
    if not new_name:
        _require_input(args, "new branch name", "--name")
        new_name = _prompt("New child branch name: ")
    if not new_name:
        raise SystemExit(1)

    # The rewind resets the worktree, so refuse tracked changes (untracked survive it).
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
        # A reset can die partway (a submodule it recursed into, say) with the index and working
        # tree already partly rewritten, so name that too: the leftover branch is the obvious
        # residue but the half-applied rewind is the one that confuses. The undo disables
        # submodule recursion, since recursion is the likeliest reason the reset just failed and
        # an undo that re-trips it is no undo at all.
        raise TreeError(
            f"Failed to rewind {branch} to {commit_hash} (see output above). '{new_name}' was "
            f"created at the old tip, and the reset may have partially applied. To undo both:\n"
            f"  git -C {top} reset --hard --no-recurse-submodules {old_head}\n"
            f"  git -C {top} branch -D {new_name}"
        )

    # New child hangs off `branch` (now at the split); `branch` keeps its own parent/fork.
    _register_child(new_name, branch, fork=commit_hash)
    # `branch`'s children were tracking its old tip, which `new_name` now carries (with the
    # full old history). Re-point them so a later propagate lands each child where it would
    # have before the split; their fork commits stay valid because `new_name` holds them.
    for c in children:
        git("config", f"branch.{c}.tree-parent-branch", new_name)

    worktree_path = _worktree_choice(args, new_name)
    _add_split_worktree(worktree_path, new_name, init_submodules=not args.no_submodule_init)

    kept_range = f"{old_fork}..{commit_hash}" if old_fork is not None else commit_hash
    kept = git_lines("log", "--oneline", kept_range)
    moved = git_lines("log", "--oneline", f"{commit_hash}..{old_head}")
    print("\nSplit complete:")
    print(f"  {branch} ({len(kept)} commits) → keeps the work up to the split")
    print(f"  {new_name} ({len(moved)} commits) → new child branch")
    if children:
        print(f"  reparented onto {new_name}: {', '.join(children)}")


def arguments(p: argparse.ArgumentParser) -> None:
    _set_completer(
        p.add_argument("--after", metavar="COMMIT", help="Commit to split after (fzf if omitted)"),
        "git_heads",
    )
    p.add_argument("--name", metavar="BRANCH", help="New branch name (prompt if omitted)")
    p.add_argument(
        "--child",
        action="store_true",
        help="Keep the current branch for the early commits; new branch takes the rest",
    )
    wt = p.add_mutually_exclusive_group()
    _set_completer(
        wt.add_argument(
            "--worktree", metavar="PATH", help="Create the new branch's worktree at PATH"
        ),
        "directories",
    )
    wt.add_argument(
        "--no-worktree", action="store_true", help="Don't create a worktree for the new branch"
    )
    p.add_argument(
        "--no-submodule-init",
        action="store_true",
        help="Skip automatic `git submodule update --init --recursive` after creating the worktree",
    )
    p.add_argument(
        "-y", "--yes", action="store_true", help="Skip the --child rewind confirmation prompt"
    )


@subcommand(
    "split",
    "Split current branch into parent + child",
    arguments=arguments,
)
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

    _add_split_worktree(worktree_path, parent_name, init_submodules=not args.no_submodule_init)

    split_range = f"{old_fork}..{commit_hash}" if old_fork is not None else commit_hash
    split_commits = git_lines("log", "--oneline", split_range)
    remaining = git_lines("log", "--oneline", f"{commit_hash}..HEAD")
    print("\nSplit complete:")
    print(f"  {parent_name} ({len(split_commits)} commits) → new parent branch")
    print(f"  {branch} ({len(remaining)} commits) → now child of {parent_name}")
