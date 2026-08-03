"""Git subprocess wrappers, config accessors, and worktree/rebase state readers."""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from git_tree._errors import TreeError


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


def _worktree_status(
    wt: Path, *, ignore_submodules: str | None = None, untracked_files: str | None = None
) -> WorktreeStatus:
    """Run `git status --porcelain` once and tally its XY codes.

    `ignore_submodules` (e.g. "none") is passed to git as `--ignore-submodules=<val>`,
    overriding the repo's `diff.ignoreSubmodules` / `submodule.<name>.ignore` config for this
    one check so a caller that must not miss dirty submodules can force them to be reported.
    `untracked_files` does the same for `status.showUntrackedFiles`. Both default to off: display
    and `--json` should report what the user's own config says, while a caller deciding whether
    deleting a worktree would destroy work must not inherit a setting that hides files.
    """
    extra = [f"--ignore-submodules={ignore_submodules}"] if ignore_submodules is not None else []
    if untracked_files is not None:
        extra.append(f"--untracked-files={untracked_files}")
    out = git("status", "--porcelain", *extra, cwd=wt)
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


def _set_fork_commit(branch: str, commit: str) -> None:
    git("config", f"branch.{branch}.tree-fork-commit", commit)


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


def _branch_remote(branch: str) -> str:
    """The branch's configured remote (branch.<name>.remote), or "" if unset."""
    return git("config", f"branch.{branch}.remote", check=False)


def _set_branch_remote(branch: str, remote: str) -> None:
    git("config", f"branch.{branch}.remote", remote)


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


def _all_branch_config() -> dict[str, dict[str, str]]:
    """Every `branch.<name>.<var>` from one `git config --list` read, as {branch: {var: value}}.

    Replaces discover()'s per-branch `git config` lookups (one per local branch, plus one per
    tree-branch) with a single subprocess. Keys are `branch.<subsection>.<var>`; the subsection
    is a branch name that may itself contain dots (e.g. `granite-4.2`), so split on the LAST dot
    for the variable and keep everything before it as the name. `-z` NUL-delimits records and
    newline-separates each key from its value, so a value containing `=` stays unambiguous.
    """
    raw = git("config", "--list", "-z", check=False)
    result: dict[str, dict[str, str]] = {}
    for record in raw.split("\0"):
        if not record.startswith("branch."):
            continue
        key, _, value = record.partition("\n")
        # Branch names may contain dots (e.g. `granite-4.2`), so split on the LAST dot:
        # the variable is the final segment, the branch name is everything before it.
        # Unambiguous only because our tree variable names contain no dot.
        name, sep, var = key[len("branch.") :].rpartition(".")
        if not sep:  # a top-level `branch.<var>` setting with no branch subsection
            continue
        result.setdefault(name, {})[var] = value
    return result


def _has_active_rebase(cwd: Path) -> bool:
    git_dir_path = _git_dir(cwd)
    return (git_dir_path / "rebase-merge").is_dir() or (git_dir_path / "rebase-apply").is_dir()


def _rebase_state_file(cwd: Path, name: str) -> str | None:
    """Read git's in-progress-rebase state file `<backend>/<name>`, trying the merge backend
    (`rebase-merge/`) first, then the legacy `am` backend (`rebase-apply/`). Returns the stripped
    contents, or None if no rebase is in progress, the file is absent, or it is empty."""
    git_dir_path = _git_dir(cwd)
    for backend in ("rebase-merge", "rebase-apply"):
        state_file = git_dir_path / backend / name
        if state_file.exists():
            return state_file.read_text().strip() or None
    return None


def _active_rebase_onto(cwd: Path) -> str | None:
    """The commit the in-progress rebase in `cwd` is replaying onto (git's `onto` state file); None
    if no active rebase. Used to tell git-tree's own cascade rebase (onto == a branch's tree-parent)
    from an unrelated hand-started one, and to record the fork at the base actually replayed."""
    return _rebase_state_file(cwd, "onto")


def _active_rebase_branch(cwd: Path) -> str | None:
    """The branch being rebased by the in-progress rebase in `cwd` (git's `head-name` state file);
    None if absent or not a `refs/heads/` ref."""
    ref = _rebase_state_file(cwd, "head-name")
    if ref and ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    return None


def _has_active_rebase_safe(cwd: Path) -> bool:
    """`_has_active_rebase`, but "cannot tell" answers no.

    It resolves the gitdir via `git rev-parse`, which exits non-zero when the worktree's `.git`
    pointer is broken or the directory is gone. Those are exactly the worktrees `rebuild` exists
    to repair, so a gate built on this must not turn them into a hard error.
    """
    try:
        return _has_active_rebase(cwd)
    except (subprocess.CalledProcessError, OSError):
        return False


class SequencerOp(StrEnum):
    """A merge, cherry-pick, or revert left in progress."""

    MERGE = "merge"
    CHERRY_PICK = "cherry-pick"
    REVERT = "revert"
    CHERRY_PICK_OR_REVERT = "cherry-pick or revert"  # `sequencer/` does not say which


# Keys are git's own state file names, so they stay plain strings: data, not tags.
_SEQUENCER_STATES = {
    "MERGE_HEAD": SequencerOp.MERGE,
    "CHERRY_PICK_HEAD": SequencerOp.CHERRY_PICK,
    "REVERT_HEAD": SequencerOp.REVERT,
    "sequencer": SequencerOp.CHERRY_PICK_OR_REVERT,
}


def _pending_sequencer_op(cwd: Path) -> SequencerOp | None:
    """The merge, cherry-pick, or revert left in progress in `cwd`, by name, else None.

    Nothing else notices these. Once the user stages their resolutions `git status` reports
    ordinary staged changes rather than `UU`, and `_rebase_branch`'s unconditional `git stash
    push` then clears the state outright, leaving an operation that cannot be continued.
    `sequencer/` catches a multi-commit cherry-pick, which has no `CHERRY_PICK_HEAD` between
    picks. Paths come from `--git-path` because they live in the per-worktree gitdir.
    """
    for name, label in _SEQUENCER_STATES.items():
        rel = git("rev-parse", "--git-path", name, cwd=cwd, check=False)
        if rel and (cwd / rel).exists():
            return label
    return None


def _is_interactive_rebase(cwd: Path) -> bool:
    """Whether the in-progress rebase is one the user started with `git rebase -i`.

    Not to be confused with git's `rebase-merge/interactive` marker file, which is useless here:
    the merge backend writes it for every rebase, git-tree's own included, so keying on it would
    refuse every cascade. Two things do tell them apart. `amend` exists only while stopped at an
    `edit` or `reword`. And git-tree's todo is machine-generated, so every verb in it is `pick`;
    any other verb was typed by a person.

    Known blind spot: a `git rebase -i` whose todo the user left as all `pick`s (reordered or
    trimmed only) is indistinguishable from git-tree's own. Driving that one forward carries out
    the user's own instructions, so it fails in the harmless direction.
    """
    if _rebase_state_file(cwd, "amend") is not None:
        return True
    for name in ("done", "git-rebase-todo"):
        for line in (_rebase_state_file(cwd, name) or "").splitlines():
            verb = line.strip().split(" ")[0]
            if verb and not verb.startswith("#") and verb not in ("pick", "p"):
                return True
    return False


class ForeignRebase(StrEnum):
    """Why a rebase in progress is not git-tree's own to drive forward."""

    NO_TREE_PARENT = "no-tree-parent"
    INTERACTIVE = "interactive"
    UNREADABLE_BASE = "unreadable-base"
    UNRELATED_BASE = "unrelated-base"


def _foreign_rebase_reason(cwd: Path, parent: str | None) -> ForeignRebase | None:
    """Why the rebase in progress at `cwd` is not git-tree's to drive, or None when it is.

    A cascade rebase aims at the branch's tree-parent, or at an earlier commit if the parent has
    moved since. Anything else belongs to the user.
    """
    if parent is None:
        return ForeignRebase.NO_TREE_PARENT
    if _is_interactive_rebase(cwd):
        return ForeignRebase.INTERACTIVE
    actual_onto = _active_rebase_onto(cwd)
    if not actual_onto:
        # `git am` records no base, so ownership is unknowable and git-tree keeps its hands off.
        return ForeignRebase.UNREADABLE_BASE
    if not git_ok("merge-base", "--is-ancestor", actual_onto, parent):
        return ForeignRebase.UNRELATED_BASE
    return None


def _foreign_rebase_phrase(reason: ForeignRebase, parent: str | None) -> str:
    """Turn a reason into wording for the error message."""
    match reason:
        case ForeignRebase.NO_TREE_PARENT:
            return "the branch has no tree-parent to rebase onto"
        case ForeignRebase.INTERACTIVE:
            return "it is an interactive rebase, not a cascade"
        case ForeignRebase.UNREADABLE_BASE:
            return "its base cannot be read (a `git am` records none)"
        case ForeignRebase.UNRELATED_BASE:
            return f"its base is neither {parent} nor an ancestor of it"


def _is_git_tree_rebase(cwd: Path, parent: str | None) -> bool:
    """Whether the rebase in progress at `cwd` is git-tree's own, so it is safe to drive forward."""
    return _foreign_rebase_reason(cwd, parent) is None


def _has_unmerged(cwd: Path) -> bool:
    """True if the worktree has unmerged (conflicted) index entries."""
    return bool(git("ls-files", "--unmerged", cwd=cwd, check=False).strip())


def _stash_push_if_created(cwd: Path) -> str | None:
    """Stash tracked changes; return the new stash commit, or None if nothing was stashed.

    Detect via `refs/stash` advancing rather than parsing git's stdout, which is
    locale-dependent ("Saved working directory ..." is only English). The SHA is what callers
    quote back to the user: `refs/stash` is shared by every worktree in the repo, so by the time
    anyone reads the advice, `stash@{0}` may be a different worktree's entry.
    """
    before = git("rev-parse", "--verify", "--quiet", "refs/stash", cwd=cwd, check=False)
    git_echo("stash", "push", cwd=cwd)
    after = git("rev-parse", "--verify", "--quiet", "refs/stash", cwd=cwd, check=False)
    return after if after and after != before else None


def _submodule_paths(worktree: Path) -> list[str]:
    """Parse .gitmodules in a worktree, returning submodule paths that exist on disk."""
    gitmodules = worktree / ".gitmodules"
    if not gitmodules.exists():
        return []
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(str(gitmodules))
    except (configparser.Error, UnicodeDecodeError, OSError) as err:
        # git accepts things configparser rejects, a repeated `[submodule "x"]` section among
        # them, and this escaping uncaught would mean a traceback and no JSON envelope. Callers
        # that use this to decide whether deleting is safe must treat it as "cannot prove clean".
        raise TreeError(f"Could not parse {gitmodules}: {err}", code=4) from err
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
