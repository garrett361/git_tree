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
from git_tree._cmd_push import cmd_push
from git_tree._cmd_rebuild import cmd_rebuild
from git_tree._cmd_remove import cmd_remove
from git_tree._cmd_skills import cmd_skills
from git_tree._cmd_tree import cmd_tree
from git_tree._display import _subtree_lines
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
    _get_tree_parent,
    _has_active_rebase,
    _is_git_tree_rebase,
    _pending_sequencer_op,
    _register_child,
    _set_fork_commit,
    _worktree_status,
    _would_cycle,
    all_branch_names,
    current_branch,
    git,
    git_echo_ok,
    git_lines,
    git_ok,
)
from git_tree._graph import (
    _get_fork_commit,
    discover,
    root_of,
)
from git_tree._guards import (
    _mid_rebase_branches,
    _require_clean_state,
    _require_healthy_submodules,
    _require_ready,
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
