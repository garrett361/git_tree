from __future__ import annotations

import argparse

import pytest

from git_tree._cmd_propagate import cmd_propagate

from .conftest import RepoHelper, cli_args


def _ns(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = dict(dry_run=False, no_auto_rerere=False, branch=None, yes=True)
    base.update(kw)
    return cli_args(**base)


def test_git_tree_rebase_does_not_move_unrelated_branch(
    repo: RepoHelper, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """With rebase.updateRefs=true a git rebase relocates *any* local branch sitting on a commit
    in the replayed range. git-tree must only move the branch it is rebasing, so an unrelated,
    non-tree branch pinned inside the range stays put."""
    repo.git("config", "rebase.updateRefs", "true")

    repo.branch("a", parent="main")
    wt_a = repo.worktree("a", str(tmp_path / "wt-a"))
    (wt_a / "a1.txt").write_text("a1")
    repo.git("add", "a1.txt", cwd=wt_a)
    repo.git("commit", "-m", "a1", cwd=wt_a)
    a1 = repo.git("rev-parse", "a")  # first commit of a, inside a's replayed range
    (wt_a / "a2.txt").write_text("a2")
    repo.git("add", "a2.txt", cwd=wt_a)
    repo.git("commit", "-m", "a2", cwd=wt_a)

    # A plain bookmark branch (no worktree, not in the tree) pinned at a1.
    repo.git("branch", "bookmark", a1)

    repo.commit("m2.txt", "m2", "advance main")

    monkeypatch.setattr("builtins.input", lambda _: "y")
    cmd_propagate(_ns())

    assert repo.git("rev-parse", "bookmark") == a1, "git tree rebase silently moved 'bookmark'"
