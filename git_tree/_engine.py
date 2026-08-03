"""The cascade engine: replaying a branch onto its parent and driving conflicts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from git_tree._errors import ConflictError, ErrorKind, TreeError
from git_tree._git import (
    _active_rebase_onto,
    _foreign_rebase_phrase,
    _foreign_rebase_reason,
    _has_active_rebase,
    _has_unmerged,
    _set_fork_commit,
    _stash_push_if_created,
    git,
    git_echo,
    git_echo_ok,
    git_lines,
    git_ok,
)
from git_tree._graph import BranchInfo, Graph, _get_fork_commit
from git_tree._guards import _refuse_unfinished_replay

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

# [empty-patch handling]
#
# git rebase --onto can exit non-zero without producing merge conflicts.
# Two distinct cases:
#
# 1. No rebase started (REBASE_HEAD absent): git determined there was nothing
#    to replay and exited. The branch ref may already be updated. Return "ok".
#
# 2. Rebase started but stopped on an empty patch (REBASE_HEAD present, no
#    unmerged files): a commit's changes are already in the target, so git
#    halted waiting for `--skip`. Loop --skip until done or a real conflict
#    appears (a multi-commit branch can hit several empty patches in a row).
#    Note: git's default merge backend (>= 2.34) auto-drops empty commits, so
#    this halt happens only under the legacy apply backend or older git. It is
#    kept defensively because git-tree pins no rebase backend and inherits the
#    user's git config; do not prune it as dead without also dropping that
#    support (verified unreachable on git 2.39's default backend).


def _replay_is_empty(cwd: Path) -> bool:
    """Whether the stopped replay produced nothing: index == HEAD and worktree == index.

    This is the precondition for `--skip`, which hard-resets. "No unmerged entries" is not the
    same thing: a resolution the user staged, or an unrelated tracked edit, also leaves no
    unmerged entries, and skipping there destroys it. Untracked files are deliberately not
    consulted, since `--skip` does not touch them. Any non-zero exit, git failing included,
    means "do not skip".
    """
    return git_ok("diff", "--quiet", "HEAD", cwd=cwd)


def _skip_empty_commits(
    cwd: Path, branch: str, stash: str | None, resume_cmd: list[str]
) -> str | None:
    """Loop --skip until rebase finishes or a real conflict appears. Returns None if
    a real conflict was hit (rebase left in progress for the user to resolve).

    Never aborts: a `--skip` that lands on a conflicting next commit is normal, so
    leave the rebase resumable rather than discarding it. Refuses outright if the stop is not
    an empty replay, since skipping would then throw work away.
    """
    while _has_active_rebase(cwd):
        if _has_unmerged(cwd):
            return None  # real conflict; leave rebase in progress
        if not _replay_is_empty(cwd):
            _refuse_unfinished_replay(branch, cwd, stash, resume_cmd)
        if not git_echo_ok("rebase", "--skip", cwd=cwd):
            return None  # --skip surfaced a conflict / can't proceed; hand to user
    return "ok (skipped empty)"


def _rerere_args(auto_rerere: bool) -> list[str]:
    """`-c rerere.enabled=...` so git-tree drives rerere by its own flag rather than the user's
    global git config. Auto-replaying a resolution across the cascade needs rerere on: the run
    where the user resolves the conflict records it, later branches with the same conflict reuse
    it. `--no-auto-rerere` turns it off."""
    return ["-c", f"rerere.enabled={'true' if auto_rerere else 'false'}"]


def _rebase_onto(
    child: str,
    parent: str,
    fork_point: str,
    cwd: Path,
    auto_rerere: bool,
    stash: str | None,
    resume_cmd: list[str],
) -> str:
    """Attempt rebase of child onto parent in its worktree. Returns status or exits on conflict."""
    head_before = git("rev-parse", "HEAD", cwd=cwd)
    rr = _rerere_args(auto_rerere)
    # -c rebase.updateRefs=false: with the user's rebase.updateRefs on, a rebase also relocates
    # any *other* local branch sitting on a commit in the replayed range. git-tree moves refs only
    # through its own propagate, so it opts out and touches just the branch it is rebasing.
    # autoSquash/rebaseMerges are opted out for a second reason: they put non-`pick` verbs
    # (`fixup`, `label`, `merge`) in the todo, which is how `_is_interactive_rebase` recognises a
    # rebase as the user's. Inheriting them would make git-tree refuse to resume its own cascade.
    result = git_echo(
        "-c",
        "rebase.updateRefs=false",
        "-c",
        "rebase.autoSquash=false",
        "-c",
        "rebase.rebaseMerges=false",
        *rr,
        "rebase",
        "--no-reapply-cherry-picks",
        "--onto",
        parent,
        fork_point,
        cwd=cwd,
    )

    if result.returncode == 0:
        return "ok"

    if not _has_active_rebase(cwd):
        # Non-zero exit, no rebase left in progress. Confirm success positively:
        # the ref must have moved. Otherwise it's a real failure (bad ref,
        # pre-rebase hook reject, ...) — don't infer "ok" from stderr text.
        if git("rev-parse", "HEAD", cwd=cwd) != head_before:
            return "ok"
        # git_echo already reprinted git's stderr above. Name the stash if one was taken: the
        # rebase never started, so nothing pops it, and the worktree just looks mysteriously
        # clean until the user goes looking.
        note = (
            f"\nYour uncommitted changes were stashed first; restore them with: "
            f"cd {cwd} && git stash apply {stash}"
            if stash
            else ""
        )
        raise TreeError(f"rebase of {child} onto {parent} failed (see output above){note}")

    return _drive_conflicted_rebase(child, parent, cwd, stash, auto_rerere, resume_cmd, rr)


def _drive_conflicted_rebase(
    child: str,
    parent: str,
    cwd: Path,
    stash: str | None,
    auto_rerere: bool,
    resume_cmd: list[str],
    rr: list[str],
) -> str:
    """A rebase in `cwd` has stopped mid-way. Skip an empty patch; else, with auto_rerere on,
    replay recorded resolutions (`rerere`), stage them, and `--continue` until it finishes;
    otherwise stop for the user via `_conflict_exit`. Returns a status string on completion."""
    if not _has_unmerged(cwd):
        status = _skip_empty_commits(cwd, child, stash, resume_cmd)
        if status is not None:
            return status
        _conflict_exit(child, parent, cwd, stash, resume_cmd)

    if not auto_rerere:
        _conflict_exit(child, parent, cwd, stash, resume_cmd)

    while True:
        git_echo(*rr, "rerere", cwd=cwd)

        remaining = git(*rr, "rerere", "remaining", cwd=cwd, check=False)
        if remaining.strip():
            _conflict_exit(child, parent, cwd, stash, resume_cmd)

        # git_echo swallows failures (check=False); a failed staging would otherwise loop
        # here forever, so treat it as an unresolvable conflict and stop.
        if not git_echo_ok("add", "-u", cwd=cwd):
            _conflict_exit(child, parent, cwd, stash, resume_cmd)

        continued = git_echo_ok(*rr, "rebase", "--continue", cwd=cwd, env={"GIT_EDITOR": "true"})
        if continued:
            return "ok (rerere)"

        # --continue stopped again — new conflict or empty patch?
        if not _has_unmerged(cwd):
            status = _skip_empty_commits(cwd, child, stash, resume_cmd)
            if status is not None:
                return "ok (rerere)"
            _conflict_exit(child, parent, cwd, stash, resume_cmd)


def _conflict_exit(
    child: str, parent: str, cwd: Path, stash: str | None, resume_cmd: list[str]
) -> NoReturn:
    # `resume_cmd` is the exact command that resumes this cascade (always a `git tree propagate
    # <branch>`); both the message and the machine `remedy` name it, and re-running it finishes
    # the rebase, so the user never runs `git rebase --continue` by hand.
    files = git_lines("diff", "--name-only", "--diff-filter=U", cwd=cwd)
    lines = [f"\nCONFLICT while rebasing {child} onto {parent}"]
    if files:
        lines.append("Conflicted files:")
        lines += [f"  {f}" for f in files]
    lines.append(f"Resolve the conflicts in {cwd} and `git add` them, then re-run:")
    lines.append(f"    {' '.join(resume_cmd)}")
    lines.append(
        "git-tree finishes the rebase for you; no need to run `git rebase --continue` yourself."
    )
    if stash:
        # By SHA, not `stash@{0}`: `refs/stash` is shared across the repo's worktrees, so that
        # index can point at another worktree's entry by the time this is read.
        lines.append(
            f"Note: dirty worktree was stashed; after resuming, run: "
            f"cd {cwd} && git stash apply {stash}"
        )
    raise ConflictError(
        "\n".join(lines),
        branch=child,
        worktree=cwd,
        conflicted_files=files,
        remedy=resume_cmd,
    )


@dataclass(frozen=True)
class RebaseResult:
    note: str  # how the rebase completed, for display: "ok", "ok (rerere)", ...
    unpopped_stash: str | None = None  # stash commit left behind when the pop conflicted


def _rebase_branch(
    branch: str,
    onto: str,
    fork_point: str,
    info: BranchInfo,
    *,
    auto_rerere: bool,
    resume_cmd: list[str],
) -> RebaseResult:
    """Rebase `branch` onto `onto` in its worktree, stashing/popping dirty changes
    and recording the new fork point. Raises (via _rebase_onto) on a real conflict,
    leaving the rebase in progress. A pop conflict is non-fatal (the branch ref is
    already rebased); it's reported via `unpopped_stash` and the worktree is left
    for the user. `resume_cmd` is the command that resumes the cascade, surfaced in
    the resume hint on conflict."""
    cwd = info.worktree
    assert cwd is not None  # callers guarantee a worktree via _require_worktrees
    stash = _stash_push_if_created(cwd) if info.is_dirty else None
    note = _rebase_onto(branch, onto, fork_point, cwd, auto_rerere, stash, resume_cmd)
    # Rebase succeeded; record the fork before the pop (which only touches the
    # working tree). `rev-parse(onto)` is stable here — rebasing `branch` never
    # moves `onto`.
    _set_fork_commit(branch, git("rev-parse", onto))
    # Keep the stash commit, not `stash@{0}`: `refs/stash` is shared by every worktree in the repo,
    # so the index the user reads later may name a different worktree's entry.
    unpopped = stash if stash and not git_echo_ok("stash", "pop", cwd=cwd) else None
    return RebaseResult(note, unpopped)


def _advance_branch(
    branch: str,
    parent: str,
    info: BranchInfo,
    fork_point: str,
    *,
    auto_rerere: bool,
    resume_cmd: list[str],
) -> RebaseResult:
    """Make `branch` rebased onto `parent`.

    Finishes an in-progress rebase if one is active in its worktree, else starts a fresh one.
    After finishing, it rebases onto `parent` as usual, so a parent that moved while the rebase
    was interrupted still reaches `branch`.
    """
    cwd = info.worktree
    assert cwd is not None
    if not _has_active_rebase(cwd):
        return _rebase_branch(
            branch, parent, fork_point, info, auto_rerere=auto_rerere, resume_cmd=resume_cmd
        )

    # RESUME: an interrupted rebase is sitting in this worktree.
    if (reason := _foreign_rebase_reason(cwd, parent)) is not None:
        # Not git-tree's cascade, so don't drive it to a base it was never aimed at.
        raise TreeError(
            f"{branch} has a rebase in progress in {cwd}: "
            f"{_foreign_rebase_phrase(reason, parent)} (git-tree did not start it). "
            f"Finish or `git rebase --abort` it there.",
            code=4,
            branches=[branch],
        )
    actual_onto = _active_rebase_onto(cwd)
    assert actual_onto is not None  # a readable base is part of the ownership test above
    if _has_unmerged(cwd):
        raise TreeError(
            f"{branch} still has unresolved conflicts in {cwd}. Resolve them and `git add` the "
            f"files, then re-run: {' '.join(resume_cmd)}",
            code=4,
            kind=ErrorKind.UNRESOLVED_CONFLICTS,
            branches=[branch],
        )
    rr = _rerere_args(auto_rerere)
    git_echo(*rr, "rebase", "--continue", cwd=cwd, env={"GIT_EDITOR": "true"})
    if _has_active_rebase(cwd):
        # `--continue` stopped again: a later commit conflicts (drive it through rerere the same
        # way a fresh rebase does), or it resolved to an empty patch to skip. Never stashes here.
        _drive_conflicted_rebase(branch, parent, cwd, None, auto_rerere, resume_cmd, rr)
    # Record the fork at the base the rebase actually replayed onto, which is where those commits
    # really landed. Naming `parent` instead would set an exclude boundary above `branch` and the
    # rebase below would replay nothing, dropping the branch's own commits. This is an
    # intermediate value that only has to survive a conflict in that rebase.
    _set_fork_commit(branch, actual_onto)
    # `parent` may have gained commits while the rebase sat interrupted, so finishing it leaves
    # `branch` behind. The ordinary propagate step from the base just recorded lands it on the
    # live parent and moves the fork with it (a no-op when `parent` did not move). It can conflict
    # in its own right, and raises through the same machinery, so `resume_cmd` still resumes it.
    onto_live = _rebase_branch(
        branch, parent, actual_onto, info, auto_rerere=auto_rerere, resume_cmd=resume_cmd
    )
    return RebaseResult("resumed", unpopped_stash=onto_live.unpopped_stash)


def _resume_cmd(branch: str) -> list[str]:
    """The argv that resumes a stalled cascade: always `git tree propagate <branch>`. A fresh list
    per call, since callers surface it as a mutable `remedy`."""
    return ["git", "tree", "propagate", branch]


def _auto_rerere(args: argparse.Namespace) -> bool:
    """Whether to auto-continue rebases via rerere (on unless `--no-auto-rerere`)."""
    return not args.no_auto_rerere


def _propagate_descendants(
    branch: str,
    graph: Graph,
    *,
    auto_rerere: bool = True,
    resume_cmd: list[str] | None = None,
) -> list[tuple[str, str]]:
    # `resume_cmd` is the command that resumes this cascade if a descendant conflicts (defaults
    # to `git tree propagate <branch>`); surfaced in the conflict hint.
    resume_cmd = resume_cmd or _resume_cmd(branch)
    descendants = graph.downstream_from(branch)
    results: list[tuple[str, str]] = []

    if descendants:
        print("Results:")
    for child in descendants:
        parent = graph.parent_of[child]
        info = graph.branches[child]
        fork_point = _get_fork_commit(child, parent, info)
        r = _advance_branch(
            child, parent, info, fork_point, auto_rerere=auto_rerere, resume_cmd=resume_cmd
        )
        text = (
            f"rebased (stash pop conflict; restore with: "
            f"cd {info.worktree} && git stash apply {r.unpopped_stash})"
            if r.unpopped_stash
            else r.note
        )
        # Stream each result as it lands: a mid-cascade conflict raises before this returns,
        # so streaming is what makes the already-rebased branches visible.
        print(f"  {child}: {text}")
        results.append((child, text))

    return results
