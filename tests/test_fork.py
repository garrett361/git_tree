"""Tests for the persisted fork point (branch.<name>.tree-fork-commit).

These cover the workflows that the stored fork makes correct and that the old
merge-base derivation gets wrong: conflict + resume, reorder/split, an external
pull --rebase that rewrites a parent, and a multi-level cascade where a middle
branch's commit content changes during its rebase.

The discriminating ingredient in every case is a parent commit whose content
changes during a rebase: that is exactly when merge-base(parent, child) drifts
off the child's true fork and the old code replays the wrong range.
"""

from __future__ import annotations

import argparse

import pytest

from git_tree.cli import (
    BranchInfo,
    _get_fork_commit,
    cmd_attach,
    cmd_branch,
    cmd_propagate,
    cmd_split,
    discover,
)

from .conftest import RepoHelper


def _ns(
    *, dry_run: bool = False, no_auto_rerere: bool = False, branch: str | None = None
) -> object:
    return argparse.Namespace(dry_run=dry_run, no_auto_rerere=no_auto_rerere, branch=branch)


def _commit_in(repo: RepoHelper, wt, filename: str, content: str, message: str) -> None:
    (wt / filename).write_text(content)
    repo.git("add", filename, cwd=wt)
    repo.git("commit", "-m", message, cwd=wt)


class TestConflictResume:
    def test_resume_continues_to_descendants(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        """Conflict at an interior branch, resolve + continue manually, re-run
        propagate; the deeper descendant rebases onto the resolved branch using
        its stored fork (only its own commit), not a drifted merge-base."""
        repo.git("config", "core.editor", "true")  # for manual `git rebase --continue`
        repo.commit("shared.txt", "original", "base shared")

        repo.git("branch", "b", "main")
        repo.set_parent("b", "main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        _commit_in(repo, wt_b, "shared.txt", "from b", "b modifies shared")

        repo.git("branch", "c", "b")
        repo.set_parent("c", "b")
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        _commit_in(repo, wt_c, "c.txt", "c", "c adds c.txt")

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="main"))

        # Resolve b's conflict by hand and finish its rebase, as a user would.
        (wt_b / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("rebase", "--continue", cwd=wt_b)

        # Re-run: must continue cleanly into c.
        cmd_propagate(_ns(branch="main"))

        assert repo.git("show", "c:shared.txt") == "resolved"
        assert repo.git("config", "branch.c.tree-fork-commit") == repo.git("rev-parse", "b")
        c_log = repo.log_oneline("c")
        assert len(c_log) == 5
        assert sum("b modifies shared" in line for line in c_log) == 1


class TestSplitAfterRewrite:
    def test_split_after_parent_rewrite_propagates_correctly(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """Rewrite a parent's commits (here via amend, as a reorder/edit would),
        split it, then propagate. The child replays only its own commit."""
        repo.commit("a.txt", "base", "base a")

        repo.git("branch", "A", "main")
        repo.set_parent("A", "main")
        wt_a = repo.worktree("A", str(tmp_path / "wt-A"))
        _commit_in(repo, wt_a, "a.txt", "first", "A1")
        _commit_in(repo, wt_a, "a.txt", "second", "A2")

        repo.git("branch", "B", "A")
        repo.set_parent("B", "A")
        wt_b = repo.worktree("B", str(tmp_path / "wt-B"))
        _commit_in(repo, wt_b, "b.txt", "b", "B commit")

        # Rewrite A's tip content (a reorder/edit of A's history).
        (wt_a / "a.txt").write_text("second-edited")
        repo.git("add", "a.txt", cwd=wt_a)
        repo.git("-c", "core.editor=true", "commit", "--amend", "-m", "A2 edited", cwd=wt_a)

        # Split A at A1 into a new parent E.
        a1_line = repo.git("log", "--oneline", "--reverse", "main..A").splitlines()[0]
        monkeypatch.setattr("git_tree.cli.fzf_select", lambda items, **kw: [a1_line])
        inputs = iter(["E", "n"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        monkeypatch.chdir(wt_a)
        cmd_split(None)

        graph = discover()
        assert graph.parent_of["A"] == "E"
        assert graph.parent_of["B"] == "A"

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="E"))

        # B carries exactly its own commit on top of the rewritten A.
        assert repo.git("rev-list", "--count", "A..B") == "1"
        assert repo.git("show", "B:b.txt") == "b"
        assert repo.git("show", "B:a.txt") == "second-edited"


class TestPullRebaseIntoParent:
    def test_external_rewrite_then_propagate(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        """A parent rewritten outside git-tree (pull --rebase replaying a local
        commit onto diverged upstream, with a conflict) still propagates
        correctly downward: the child's stored fork is untouched."""
        repo.git("config", "core.editor", "true")

        repo.git("branch", "A", "main")
        repo.set_parent("A", "main")
        wt_a = repo.worktree("A", str(tmp_path / "wt-A"))
        _commit_in(repo, wt_a, "a.txt", "a1", "A1")
        repo.git("push", "-u", "origin", "A", cwd=wt_a)
        _commit_in(repo, wt_a, "a.txt", "a2-local", "A2")  # local, unpushed

        repo.git("branch", "B", "A")
        repo.set_parent("B", "A")
        wt_b = repo.worktree("B", str(tmp_path / "wt-B"))
        _commit_in(repo, wt_b, "b.txt", "b", "B commit")
        old_a2 = repo.git("rev-parse", "A")

        # A teammate appends a conflicting commit to origin/A.
        clone2 = tmp_path / "clone2"
        repo.git("clone", str(repo.origin), str(clone2), cwd=tmp_path)
        repo.git("config", "user.email", "t@t.com", cwd=clone2)
        repo.git("config", "user.name", "t", cwd=clone2)
        repo.git("checkout", "A", cwd=clone2)
        _commit_in(repo, clone2, "a.txt", "a-team", "teammate on A")
        repo.git("push", "origin", "A", cwd=clone2)

        # pull --rebase replays the local A2 onto the teammate commit -> conflict.
        repo.git("pull", "--rebase", "origin", "A", cwd=wt_a, check=False)
        (wt_a / "a.txt").write_text("a2-resolved")
        repo.git("add", "a.txt", cwd=wt_a)
        repo.git("rebase", "--continue", cwd=wt_a)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="A"))

        assert repo.git("rev-list", "--count", "A..B") == "1"
        assert old_a2 not in repo.git("rev-list", "B")
        assert repo.git("config", "branch.B.tree-fork-commit") == repo.git("rev-parse", "A")
        assert repo.git("show", "B:b.txt") == "b"
        assert repo.git("show", "B:a.txt") == "a2-resolved"


class TestCascadeModifiedMiddle:
    def test_middle_commit_modified_by_rerere(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """3-deep cascade where the middle branch's commit content changes during
        its rebase (rerere auto-resolution). The leaf inherits the resolved
        content with no duplicated middle commit."""
        repo.enable_rerere()
        repo.commit("shared.txt", "original", "base")

        repo.git("branch", "b", "main")
        repo.set_parent("b", "main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        _commit_in(repo, wt_b, "shared.txt", "from b", "b modifies shared")
        b_original = repo.git("rev-parse", "b")

        repo.git("branch", "c", "b")
        repo.set_parent("c", "b")
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        _commit_in(repo, wt_c, "c.txt", "c", "c adds c.txt")

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")

        # Record b's rerere resolution, then restore b to its un-rebased state.
        repo.git("rebase", "--onto", "main", b_original + "~1", cwd=wt_b, check=False)
        (wt_b / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("rebase", "--continue", cwd=wt_b)
        repo.git("reset", "--hard", b_original, cwd=wt_b)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="main"))

        assert repo.git("show", "c:shared.txt") == "resolved"
        c_log = repo.log_oneline("c")
        assert len(c_log) == 5
        assert sum("b modifies shared" in line for line in c_log) == 1


class TestForkCommitLifecycle:
    def test_set_on_branch(self, repo: RepoHelper, tmp_path) -> None:
        parent_tip = repo.git("rev-parse", "main")
        cmd_branch(argparse.Namespace(name="feat", path=str(tmp_path / "wt-feat")))
        assert repo.git("config", "branch.feat.tree-parent-branch") == "main"
        assert repo.git("config", "branch.feat.tree-fork-commit") == parent_tip

    def test_set_on_attach(self, repo: RepoHelper) -> None:
        repo.git("branch", "feat")
        repo.checkout("feat")
        repo.commit("f.txt", "f", "on feat")
        expected = repo.git("merge-base", "main", "feat")
        cmd_attach(argparse.Namespace(parent="main"))
        assert repo.git("config", "branch.feat.tree-fork-commit") == expected

    def test_set_on_split(self, repo: RepoHelper, monkeypatch) -> None:
        repo.git("branch", "feat", "main")
        repo.set_parent("feat", "main")
        repo.checkout("feat")
        main_tip = repo.git("rev-parse", "main")
        repo.commit("f1.txt", "f1", "f1")
        repo.commit("f2.txt", "f2", "f2")

        split_line = repo.git("log", "--oneline", "--reverse", "main..feat").splitlines()[0]
        boundary = repo.git("rev-parse", split_line.split()[0])
        monkeypatch.setattr("git_tree.cli.fzf_select", lambda items, **kw: [split_line])
        inputs = iter(["feat-base", "n"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        cmd_split(None)

        # New parent inherits feat's old fork (where it forked from main);
        # feat now forks from the split boundary.
        assert repo.git("config", "branch.feat-base.tree-fork-commit") == main_tip
        assert repo.git("config", "branch.feat.tree-fork-commit") == boundary

    def test_updated_after_propagate(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.git("branch", "b", "main")
        repo.set_parent("b", "main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        _commit_in(repo, wt_b, "b1.txt", "b1", "b commit")
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")
        main_tip = repo.git("rev-parse", "main")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="main"))

        assert repo.git("config", "branch.b.tree-fork-commit") == main_tip


class TestForkAncestorGuard:
    """The stored fork is honored only when it is an ancestor of the branch; otherwise
    _get_fork_commit falls back to merge-base so the --onto range stays correct."""

    def _build_off_line_fork(self, repo: RepoHelper) -> str:
        """main=c0; child=c0+c1+c2 (fork=c0); side=c0+c1+s. Return side's tip, which
        shares c1 with child but is NOT on child's line (non-ancestral)."""
        repo.git("checkout", "-b", "child")
        repo.commit("g.txt", "c1", "c1")
        c1 = repo.head
        repo.commit("h.txt", "c2", "c2")
        repo.checkout("main")
        repo.set_parent("child", "main")  # fork = merge-base(main, child) = c0
        repo.git("checkout", c1, "-b", "side")
        repo.commit("s.txt", "s", "side commit")
        side_tip = repo.head
        repo.checkout("main")
        return side_tip

    def test_non_ancestral_stored_fork_falls_back_to_merge_base(self, repo: RepoHelper) -> None:
        side_tip = self._build_off_line_fork(repo)
        repo.git("config", "branch.child.tree-fork-commit", side_tip)

        mb = repo.git("merge-base", "main", "child")
        assert _get_fork_commit("child", "main") == mb
        assert _get_fork_commit("child", "main") != side_tip

    def test_non_ancestral_guard_applies_on_info_path(self, repo: RepoHelper) -> None:
        # The path propagate actually uses: stored fork arrives via BranchInfo.
        side_tip = self._build_off_line_fork(repo)
        info = BranchInfo(name="child", fork_commit=side_tip)

        mb = repo.git("merge-base", "main", "child")
        assert _get_fork_commit("child", "main", info) == mb

    def test_ancestral_fork_honored_despite_merge_base_drift(self, repo: RepoHelper) -> None:
        # main=c0+m1; b=c0+m1+b1 with fork=m1. Reword m1 so merge-base(main, b) drifts
        # back to c0, but m1 stays an ancestor of b — the stored fork must be honored.
        repo.commit("m.txt", "m1", "m1")
        m1 = repo.head
        repo.branch("b", parent="main")  # fork = merge-base(main, b) = m1
        repo.checkout("b")
        repo.commit("b1.txt", "b1", "b1")
        repo.checkout("main")
        repo.git("commit", "--amend", "-m", "m1 reworded")

        mb = repo.git("merge-base", "main", "b")
        assert mb != m1  # merge-base drifted below the fork
        assert _get_fork_commit("b", "main") == m1

    def test_fork_equal_to_branch_tip_is_kept(self, repo: RepoHelper) -> None:
        # A fork equal to the branch tip is its own ancestor (empty replay range); it
        # must stay stored, not be downgraded to merge-base.
        repo.git("checkout", "-b", "b")
        repo.commit("b1.txt", "b1", "b1")
        tip = repo.head
        repo.checkout("main")
        repo.git("config", "branch.b.tree-fork-commit", tip)

        assert _get_fork_commit("b", "main") == tip


class TestCleanCascade:
    def test_three_deep_no_duplicate_commits(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.git("branch", "b", "main")
        repo.set_parent("b", "main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        _commit_in(repo, wt_b, "b1.txt", "b1", "b commit")

        repo.git("branch", "c", "b")
        repo.set_parent("c", "b")
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        _commit_in(repo, wt_c, "c1.txt", "c1", "c commit")

        repo.git("branch", "d", "c")
        repo.set_parent("d", "c")
        wt_d = repo.worktree("d", str(tmp_path / "wt-d"))
        _commit_in(repo, wt_d, "d1.txt", "d1", "d commit")

        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="main"))

        assert len(repo.log_oneline("b")) == 3
        assert len(repo.log_oneline("c")) == 4
        assert len(repo.log_oneline("d")) == 5
        for ref, msg in (("b", "b commit"), ("c", "c commit"), ("d", "d commit")):
            assert sum(msg in line for line in repo.log_oneline(ref)) == 1
        # The advanced main commit reached every descendant exactly once.
        for ref in ("b", "c", "d"):
            assert sum("advance main" in line for line in repo.log_oneline(ref)) == 1
