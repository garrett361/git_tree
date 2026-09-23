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

import json

import pytest

from git_tree._cmd_attach import cmd_attach
from git_tree._cmd_branch import cmd_branch
from git_tree._cmd_propagate import cmd_propagate
from git_tree._cmd_rebase import cmd_rebase
from git_tree._cmd_split import cmd_split
from git_tree._errors import ConflictError, TreeError
from git_tree._graph import BranchInfo, _get_fork_commit, discover
from git_tree.cli import main

from .conftest import RepoHelper, cli_args


def _ns(
    *, dry_run: bool = False, no_auto_rerere: bool = False, branch: str | None = None
) -> object:
    return cli_args(dry_run=dry_run, no_auto_rerere=no_auto_rerere, branch=branch)


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
        self, repo: RepoHelper, monkeypatch, pick_fzf, tmp_path
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
        pick_fzf(a1_line)
        inputs = iter(["E", "n"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        monkeypatch.chdir(wt_a)
        cmd_split(cli_args(command="split"))

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
        cmd_branch(cli_args(command="branch", name="feat", path=str(tmp_path / "wt-feat")))
        assert repo.git("config", "branch.feat.tree-parent-branch") == "main"
        assert repo.git("config", "branch.feat.tree-fork-commit") == parent_tip

    def test_set_on_attach(self, repo: RepoHelper) -> None:
        repo.git("branch", "feat")
        repo.checkout("feat")
        repo.commit("f.txt", "f", "on feat")
        expected = repo.git("merge-base", "main", "feat")
        cmd_attach(cli_args(command="attach", parent="main"))
        assert repo.git("config", "branch.feat.tree-fork-commit") == expected

    def test_set_on_split(self, repo: RepoHelper, monkeypatch, pick_fzf) -> None:
        repo.git("branch", "feat", "main")
        repo.set_parent("feat", "main")
        repo.checkout("feat")
        main_tip = repo.git("rev-parse", "main")
        repo.commit("f1.txt", "f1", "f1")
        repo.commit("f2.txt", "f2", "f2")

        split_line = repo.git("log", "--oneline", "--reverse", "main..feat").splitlines()[0]
        boundary = repo.git("rev-parse", split_line.split()[0])
        pick_fzf(split_line)
        inputs = iter(["feat-base", "n"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        cmd_split(cli_args(command="split"))

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
        mb = repo.git("merge-base", "main", "child")

        # Config path: a non-ancestral fork stored on disk is ignored.
        repo.git("config", "branch.child.tree-fork-commit", side_tip)
        assert _get_fork_commit("child", "main") == mb
        assert _get_fork_commit("child", "main") != side_tip

        # BranchInfo path (the one propagate actually uses): stored fork arrives via info.
        info = BranchInfo(name="child", fork_commit=side_tip)
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


def _stale_child(repo: RepoHelper, monkeypatch, tmp_path) -> tuple[str, str]:
    """Reproduce a child attached to a rewritten parent it holds old copies of.

    P = [A, B] off main and C = P + [C1, C2]. P is then rewritten to [X, A', B', Z]: A' and B'
    are patch-identical cherry-picks of A and B, and Z edits the line they touch. Attaching C
    afterwards records merge-base(P, C) = main as its fork, so a cascade would replay A and B
    onto Z and conflict. Returns (C's copy of B, which is the boundary to replay from; C's tip)."""
    repo.commit("f.txt", "0", "base f")
    repo.branch("P", parent="main")
    wt_p = repo.worktree("P", str(tmp_path / "wt-P"))
    _commit_in(repo, wt_p, "f.txt", "a", "A")
    _commit_in(repo, wt_p, "f.txt", "b", "B")
    b_copy = repo.git("rev-parse", "P")

    repo.git("branch", "C", "P")
    wt_c = repo.worktree("C", str(tmp_path / "wt C"))
    _commit_in(repo, wt_c, "c1.txt", "c1", "C1")
    _commit_in(repo, wt_c, "c2.txt", "c2", "C2")

    repo.git("reset", "--hard", "main", cwd=wt_p)
    _commit_in(repo, wt_p, "x.txt", "x", "X")
    repo.git("cherry-pick", f"{b_copy}~1", b_copy, cwd=wt_p)
    _commit_in(repo, wt_p, "f.txt", "z", "Z")

    monkeypatch.chdir(wt_c)
    cmd_attach(cli_args(parent="P"))
    monkeypatch.chdir(repo.work)
    return b_copy, repo.git("rev-parse", "C")


def _json_stale_forks(capsys) -> dict[str, str | None]:
    capsys.readouterr()
    main(["--json"])
    return {b["name"]: b["stale_fork"] for b in json.loads(capsys.readouterr().out)["branches"]}


class TestStaleFork:
    """A child whose fork leaves copies of its parent's commits at the front of the replay."""

    def test_fixture_really_conflicts_without_the_guard(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        _stale_child(repo, monkeypatch, tmp_path)
        with pytest.raises(ConflictError):
            cmd_propagate(cli_args(branch="P", yes=True, allow_stale_fork=True))

    def test_propagate_refuses_before_rewriting(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        b_copy, c_tip = _stale_child(repo, monkeypatch, tmp_path)
        with pytest.raises(TreeError) as exc:
            cmd_propagate(cli_args(branch="P", yes=True))

        assert exc.value.kind == "stale_fork"
        assert exc.value.code == 4
        assert exc.value.branches == ["C"]
        assert f"git -C '{tmp_path / 'wt C'}' tree attach P --fork {b_copy}," in exc.value.message
        assert repo.git("rev-parse", "C") == c_tip

    def test_printed_attach_remedy_replays_only_own_commits(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        b_copy, _ = _stale_child(repo, monkeypatch, tmp_path)
        monkeypatch.chdir(tmp_path / "wt C")
        cmd_attach(cli_args(parent="P", fork=b_copy))
        monkeypatch.chdir(repo.work)

        cmd_propagate(cli_args(branch="P", yes=True))

        assert repo.git("log", "--format=%s", "P..C").splitlines() == ["C2", "C1"]
        assert repo.git("show", "C:f.txt") == "z"

    def test_rebase_refuses_the_named_branch_and_its_fork_flag_fixes_it(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        b_copy, c_tip = _stale_child(repo, monkeypatch, tmp_path)
        with pytest.raises(TreeError) as exc:
            cmd_rebase(cli_args(target="P", branch="C", yes=True))
        assert exc.value.kind == "stale_fork"
        assert f"git tree rebase P C --fork {b_copy}\n" in exc.value.message
        assert repo.git("rev-parse", "C") == c_tip

        cmd_rebase(cli_args(target="P", branch="C", yes=True, fork=b_copy[:9]))

        assert repo.git("log", "--format=%s", "P..C").splitlines() == ["C2", "C1"]

    def test_rebase_onto_another_target_honors_allow_stale_fork(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        b_copy, c_tip = _stale_child(repo, monkeypatch, tmp_path)
        repo.git("branch", "T", "P")
        with pytest.raises(TreeError) as exc:
            cmd_rebase(cli_args(target="T", branch="C", yes=True))
        assert exc.value.kind == "stale_fork"
        assert f"git tree rebase T C --fork {b_copy}\n" in exc.value.message
        assert repo.git("rev-parse", "C") == c_tip

        with pytest.raises(ConflictError):
            cmd_rebase(cli_args(target="T", branch="C", yes=True, allow_stale_fork=True))

    def test_fully_edited_copies_are_not_flagged(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        """Copies that match the parent only by subject are replayed, never dropped."""
        repo.commit("f.txt", "0", "base f")
        repo.branch("P", parent="main")
        wt_p = repo.worktree("P", str(tmp_path / "wt-P"))
        _commit_in(repo, wt_p, "f.txt", "a", "A")
        _commit_in(repo, wt_p, "g.txt", "b", "B")
        repo.git("branch", "C", "P")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        _commit_in(repo, wt_c, "c1.txt", "c1", "C1")
        repo.git("reset", "--hard", "main", cwd=wt_p)
        _commit_in(repo, wt_p, "f.txt", "a-edited", "A")
        _commit_in(repo, wt_p, "g.txt", "b-edited", "B")
        monkeypatch.chdir(wt_c)
        cmd_attach(cli_args(parent="P"))
        monkeypatch.chdir(repo.work)

        assert _json_stale_forks(capsys)["C"] is None
        with pytest.raises(ConflictError):
            cmd_propagate(cli_args(branch="P", yes=True))

    def test_boundary_stops_at_the_last_exact_copy(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        repo.branch("P", parent="main")
        wt_p = repo.worktree("P", str(tmp_path / "wt-P"))
        _commit_in(repo, wt_p, "f.txt", "a", "A")
        a_copy = repo.git("rev-parse", "P")
        _commit_in(repo, wt_p, "g.txt", "b", "B")
        repo.git("branch", "C", "P")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        _commit_in(repo, wt_c, "c1.txt", "c1", "C1")
        repo.git("reset", "--hard", "main", cwd=wt_p)
        _commit_in(repo, wt_p, "x.txt", "x", "X")
        repo.git("cherry-pick", a_copy, cwd=wt_p)
        _commit_in(repo, wt_p, "g.txt", "b-edited", "B")
        monkeypatch.chdir(wt_c)
        cmd_attach(cli_args(parent="P"))
        monkeypatch.chdir(repo.work)

        assert _json_stale_forks(capsys)["C"] == a_copy
        monkeypatch.chdir(wt_c)
        cmd_attach(cli_args(parent="P", fork=a_copy))
        monkeypatch.chdir(repo.work)
        with pytest.raises(ConflictError):
            cmd_propagate(cli_args(branch="P", yes=True))

    def test_a_child_made_only_of_copies_resets_onto_its_parent(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        repo.branch("P", parent="main")
        wt_p = repo.worktree("P", str(tmp_path / "wt-P"))
        _commit_in(repo, wt_p, "f.txt", "a", "A")
        _commit_in(repo, wt_p, "g.txt", "b", "B")
        old_p = repo.git("rev-parse", "P")
        repo.git("branch", "C", "P")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        repo.git("reset", "--hard", "main", cwd=wt_p)
        repo.git("cherry-pick", old_p, f"{old_p}~1", cwd=wt_p)
        monkeypatch.chdir(wt_c)
        cmd_attach(cli_args(parent="P"))
        monkeypatch.chdir(repo.work)

        assert _json_stale_forks(capsys)["C"] == old_p
        monkeypatch.chdir(wt_c)
        cmd_attach(cli_args(parent="P", fork=old_p))
        monkeypatch.chdir(repo.work)
        cmd_propagate(cli_args(branch="P", yes=True))

        assert repo.git("rev-parse", "C") == repo.git("rev-parse", "P")

    def test_two_shared_generic_subjects_are_not_stale(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        """A healthy child whose own commits share subjects, not patches, with its rebased
        parent is an ordinary propagate."""
        repo.branch("P", parent="main")
        wt_p = repo.worktree("P", str(tmp_path / "wt-P"))
        _commit_in(repo, wt_p, "p1.txt", "p1", "wip")
        _commit_in(repo, wt_p, "p2.txt", "p2", "wip")
        repo.git("branch", "C", "P")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        _commit_in(repo, wt_c, "c1.txt", "c1", "wip")
        _commit_in(repo, wt_c, "c2.txt", "c2", "wip")
        monkeypatch.chdir(wt_c)
        cmd_attach(cli_args(parent="P"))
        monkeypatch.chdir(repo.work)
        repo.commit("m.txt", "m", "advance main")
        repo.git("rebase", "main", cwd=wt_p)

        assert _json_stale_forks(capsys)["C"] is None
        cmd_propagate(cli_args(branch="P", yes=True))

        assert repo.git("log", "--format=%s", "P..C").splitlines() == ["wip", "wip"]
        assert repo.git("show", "C:c2.txt") == "c2"

    def test_own_commits_after_an_upstreamed_copy_are_kept(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        repo.branch("P", parent="main")
        wt_p = repo.worktree("P", str(tmp_path / "wt-P"))
        _commit_in(repo, wt_p, "p1.txt", "p1", "wip")
        _commit_in(repo, wt_p, "p2.txt", "p2", "wip")
        repo.git("branch", "C", "P")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        _commit_in(repo, wt_c, "h.txt", "h", "hotfix")
        hotfix = repo.git("rev-parse", "C")
        _commit_in(repo, wt_c, "c1.txt", "c1", "wip")
        _commit_in(repo, wt_c, "c2.txt", "c2", "wip")
        _commit_in(repo, wt_c, "feat.txt", "f", "feature")
        monkeypatch.chdir(wt_c)
        cmd_attach(cli_args(parent="P"))
        monkeypatch.chdir(repo.work)
        repo.git("cherry-pick", hotfix)
        repo.commit("m.txt", "m", "advance main")
        repo.git("rebase", "main", cwd=wt_p)

        assert _json_stale_forks(capsys)["C"] == hotfix
        monkeypatch.chdir(wt_c)
        cmd_attach(cli_args(parent="P", fork=hotfix))
        monkeypatch.chdir(repo.work)
        cmd_propagate(cli_args(branch="P", yes=True))

        assert repo.git("log", "--format=%s", "P..C").splitlines() == ["feature", "wip", "wip"]

    def test_one_shared_generic_subject_is_not_stale(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """A drifted child whose first commit shares a subject, not a patch, with a new parent
        commit is an ordinary propagate."""
        repo.branch("P", parent="main")
        wt_p = repo.worktree("P", str(tmp_path / "wt-P"))
        _commit_in(repo, wt_p, "p.txt", "p", "P1")
        repo.git("branch", "C", "P")
        repo.set_parent("C", "P")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        _commit_in(repo, wt_c, "c.txt", "c", "wip")
        _commit_in(repo, wt_c, "c2.txt", "c2", "C2")
        _commit_in(repo, wt_p, "q.txt", "q", "wip")

        cmd_propagate(cli_args(branch="P", yes=True))

        assert repo.git("log", "--format=%s", "P..C").splitlines() == ["C2", "wip"]

    def test_json_forest_reports_the_boundary(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        b_copy, _ = _stale_child(repo, monkeypatch, tmp_path)
        capsys.readouterr()
        main(["--json"])
        branches = {b["name"]: b for b in json.loads(capsys.readouterr().out)["branches"]}

        assert branches["C"]["stale_fork"] == b_copy
        assert branches["P"]["stale_fork"] is None
