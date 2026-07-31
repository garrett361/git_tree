"""Rebase/stash engine robustness (Unit 1)."""

from __future__ import annotations

import pytest

from git_tree.cli import _stash_push_if_created, cmd_propagate

from .conftest import RepoHelper, cli_args


def _ns(*, branch: str | None = None) -> object:
    return cli_args(dry_run=False, no_auto_rerere=False, branch=branch)


def _commit_in(repo: RepoHelper, wt, filename: str, content: str, message: str) -> None:
    (wt / filename).write_text(content)
    repo.git("add", filename, cwd=wt)
    repo.git("commit", "-m", message, cwd=wt)


class TestStashDetection:
    def test_returns_the_new_stash_only_when_one_is_created(self, repo: RepoHelper) -> None:
        # Clean tree: nothing stashed.
        assert _stash_push_if_created(repo.work) is None

        # Untracked-only: `git stash push` (no -u) creates nothing.
        (repo.work / "untracked.txt").write_text("u")
        assert _stash_push_if_created(repo.work) is None
        (repo.work / "untracked.txt").unlink()

        # Tracked change: a stash is created (locale-independent detection), and the caller gets
        # its SHA, since `refs/stash` is shared repo-wide and `stash@{0}` can shift under it.
        (repo.work / "init.txt").write_text("changed")
        created = _stash_push_if_created(repo.work)
        assert created == repo.git("rev-parse", "refs/stash")
        assert repo.git("stash", "list")  # a stash entry now exists
        # working tree restored by the stash
        assert repo.git("status", "--porcelain") == ""


class TestRebaseFailureReported:
    def test_pre_rebase_hook_rejection_is_not_reported_ok(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """A rebase that fails without conflict (pre-rebase hook reject, message
        not prefixed error:/fatal:) must raise, not be swallowed as success."""
        repo.git("branch", "b", "main")
        repo.set_parent("b", "main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        _commit_in(repo, wt_b, "b1.txt", "b1", "b commit")

        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")

        # Hooks live in the shared common git dir; applies to the linked worktree.
        hook = repo.work / ".git" / "hooks" / "pre-rebase"
        hook.write_text("#!/bin/sh\necho 'refusing to rebase'\nexit 1\n")
        hook.chmod(0o755)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="main"))

        # b was not actually moved onto main (the rebase was rejected).
        assert "advance main" not in repo.git("log", "--oneline", "b")

    def test_stash_is_named_when_the_rebase_never_starts(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        """A rebase rejected before it starts leaves the auto-stash with nothing to pop it.

        `_conflict_exit` names the stash, but this path raises earlier and used to say nothing,
        so the worktree just looked mysteriously clean and the work sat in a stash the user had
        no reason to look for.
        """
        repo.git("branch", "b", "main")
        repo.set_parent("b", "main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        _commit_in(repo, wt_b, "b1.txt", "b1", "b commit")
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")

        (wt_b / "b1.txt").write_text("UNCOMMITTED WORK")  # gets stashed before the rebase

        hook = repo.work / ".git" / "hooks" / "pre-rebase"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="main"))

        stash = repo.git("rev-parse", "refs/stash")
        assert stash  # the work is recoverable...
        assert stash in capsys.readouterr().err  # ...and the error says exactly where
