from __future__ import annotations

import subprocess

import pytest

from git_tree._cmd_attach import cmd_attach
from git_tree._cmd_detach import cmd_detach
from git_tree._errors import TreeError
from git_tree._graph import discover, roots

from .conftest import RepoHelper, _git, cli_args


def _ns(**kwargs) -> object:
    return cli_args(**kwargs)


class TestAttach:
    def test_sets_tree_parent(self, repo: RepoHelper) -> None:
        repo.git("branch", "feature")
        repo.checkout("feature")
        cmd_attach(_ns(parent="main"))
        graph = discover()
        assert graph.parent_of["feature"] == "main"

    def test_overwrites_existing_parent(self, repo: RepoHelper) -> None:
        repo.git("branch", "feature")
        repo.git("branch", "other")
        repo.set_parent("feature", "main")
        repo.checkout("feature")
        cmd_attach(_ns(parent="other"))
        graph = discover()
        assert graph.parent_of["feature"] == "other"

    def test_attach_to_branch_with_parent_chain_succeeds(self, repo: RepoHelper) -> None:
        # main <- base <- mid; attaching a fresh `feature` under mid makes the cycle
        # walk climb mid -> base -> main without finding feature. Must NOT be blocked.
        repo.branch("base", parent="main")
        repo.branch("mid", parent="base")
        repo.git("branch", "feature")
        repo.checkout("feature")
        cmd_attach(_ns(parent="mid"))
        assert discover().parent_of["feature"] == "mid"

    def test_self_attach_raises_and_writes_no_config(self, repo: RepoHelper) -> None:
        repo.git("branch", "feature")
        repo.checkout("feature")
        with pytest.raises(TreeError):
            cmd_attach(_ns(parent="feature"))
        result = subprocess.run(
            ["git", "config", "branch.feature.tree-parent-branch"],
            cwd=repo.work,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0

    def test_attach_to_descendant_raises_keeps_parent(self, repo: RepoHelper) -> None:
        # main <- a <- b; attaching a under its own descendant b would loop. Must raise
        # and leave a's parent unchanged.
        repo.branch("a", parent="main")
        repo.branch("b", parent="a")
        repo.checkout("a")
        with pytest.raises(TreeError):
            cmd_attach(_ns(parent="b"))
        assert discover().parent_of["a"] == "main"

    def test_invalid_parents_raise_before_side_effects(
        self, repo: RepoHelper, monkeypatch, capsys
    ) -> None:
        # A tree-parent must be a local branch: discover() drops any other edge and reports the
        # child as orphaned, so each of these must be refused with nothing written.
        repo.git("tag", "v1")
        repo.git("branch", "feature")
        repo.checkout("feature")

        # input must never be consulted — the parent is given, so nothing may prompt.
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("prompted"))

        for bad in ("v1", "origin/main", "no-such-branch"):
            with pytest.raises(TreeError) as exc:
                cmd_attach(_ns(parent=bad))
            assert exc.value.code == 4
            assert "is not a local branch" in capsys.readouterr().err
            assert "feature" not in discover().parent_of

    def test_attach_disjoint_history_clean_error(self, repo: RepoHelper, capsys, tmp_path) -> None:
        """Attaching to a branch with no common history is a TreeError, not a traceback.

        Reaches _register_child's own "No common history" guard, which cmd_branch's
        separate guard does not exercise (cmd_attach is the only caller that can reach it
        without a precomputed fork)."""
        orphan_wt = tmp_path / "orphan-wt"
        repo.git("worktree", "add", "--detach", str(orphan_wt))
        _git("checkout", "--orphan", "orphan", cwd=orphan_wt)
        (orphan_wt / "o.txt").write_text("orphan")
        _git("add", "o.txt", cwd=orphan_wt)
        _git("commit", "-m", "orphan root", cwd=orphan_wt)
        repo.git("worktree", "remove", str(orphan_wt))

        repo.checkout("orphan")
        with pytest.raises(TreeError):
            cmd_attach(_ns(parent="main"))

        err = capsys.readouterr().err
        assert "No common history" in err

    def test_reattach_to_a_parent_with_disjoint_history_clean_error(
        self, repo: RepoHelper, capsys, tmp_path
    ) -> None:
        """A recorded fork is only kept against a parent that still shares history."""
        orphan_wt = tmp_path / "orphan-wt"
        repo.git("worktree", "add", "--detach", str(orphan_wt))
        _git("checkout", "--orphan", "orphan", cwd=orphan_wt)
        (orphan_wt / "o.txt").write_text("orphan")
        _git("add", "o.txt", cwd=orphan_wt)
        _git("commit", "-m", "orphan root", cwd=orphan_wt)
        repo.git("worktree", "remove", str(orphan_wt))
        repo.git("config", "branch.orphan.tree-parent-branch", "main")
        repo.git("config", "branch.orphan.tree-fork-commit", repo.git("rev-parse", "orphan"))

        repo.checkout("orphan")
        with pytest.raises(TreeError):
            cmd_attach(_ns(parent="main"))

        assert "No common history" in capsys.readouterr().err


class TestAttachForkCommit:
    def test_reattaching_to_the_same_parent_keeps_a_valid_fork(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        """Attach is advertised as recording an edge, so it must not quietly widen the replay set.

        main gains M1, b forks there and adds B1, then main is rewritten so M1 leaves its history.
        The recorded fork still bounds b's own commits; merge-base no longer does.
        """
        repo.commit("m.txt", "m1", "M1")
        m1 = repo.git("rev-parse", "HEAD")
        repo.branch("b", parent="main")
        repo.git("config", "branch.b.tree-fork-commit", m1)
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "B1", cwd=wt_b)

        repo.git("commit", "--amend", "-m", "M1 rewritten")  # main rewritten; M1 orphaned
        assert repo.git("merge-base", "main", "b") != m1  # merge-base has drifted below it

        monkeypatch.chdir(wt_b)  # attach acts on the current branch
        cmd_attach(_ns(parent="main"))

        assert repo.git("config", "--get", "branch.b.tree-fork-commit") == m1
        assert f"(kept fork {m1[:9]})" in capsys.readouterr().out
        fork = repo.git("config", "--get", "branch.b.tree-fork-commit")
        assert repo.git("log", "--oneline", f"{fork}..b").count("\n") == 0  # B1 alone

    def test_reattaching_replaces_a_fork_below_the_merge_base(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """A child already rebased onto its parent with plain git re-attaches at the parent tip,
        rather than keeping an older fork that would replay the parent's commits."""
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "B1", cwd=wt_b)
        repo.commit("f.txt", "p1", "P1")
        repo.commit("f.txt", "p2", "P2")
        repo.git("rebase", "main", cwd=wt_b)

        monkeypatch.chdir(wt_b)
        cmd_attach(_ns(parent="main"))

        assert repo.git("config", "--get", "branch.b.tree-fork-commit") == repo.head

    def test_reattaching_keeps_a_fork_beside_the_merge_base(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """The parent was rewritten and then merged into the branch, so the recorded fork is
        incomparable with the merge-base; it still bounds the branch's own commits."""
        repo.commit("p.txt", "p", "P1")
        p1 = repo.head
        repo.branch("b", parent="main")
        repo.git("config", "branch.b.tree-fork-commit", p1)
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "B1", cwd=wt_b)
        repo.git("commit", "--amend", "-m", "P1 rewritten")
        repo.git("merge", "--no-edit", "main", cwd=wt_b)

        monkeypatch.chdir(wt_b)
        cmd_attach(_ns(parent="main"))

        assert repo.git("config", "--get", "branch.b.tree-fork-commit") == p1

    def test_attaching_to_a_new_parent_recomputes_the_fork(self, repo: RepoHelper) -> None:
        """The old fork bounds the replay against the old parent, so a new parent discards it."""
        main_tip = repo.head
        repo.git("checkout", "-b", "a")
        repo.commit("a1.txt", "a1", "A1")
        a_tip = repo.head
        repo.git("checkout", "-b", "b")
        repo.commit("b1.txt", "b1", "B1")
        repo.set_parent("b", "main")
        repo.git("config", "branch.b.tree-fork-commit", main_tip)

        cmd_attach(_ns(parent="a"))

        assert repo.git("config", "--get", "branch.b.tree-fork-commit") == a_tip

    def test_fork_flag_records_the_given_commit(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        """An explicit --fork is recorded as given, and silences the descent warning it answers."""
        repo.git("checkout", "-b", "b")
        repo.commit("b1.txt", "b1", "B1")
        b1 = repo.head
        repo.commit("b2.txt", "b2", "B2")
        repo.checkout("main")
        repo.commit("m.txt", "m", "M")  # main moves on, so b no longer descends from it

        monkeypatch.chdir(repo.worktree("b", str(tmp_path / "wt-b")))
        cmd_attach(_ns(parent="main", fork=b1[:9]))

        assert repo.git("config", "--get", "branch.b.tree-fork-commit") == b1
        assert "does not appear to descend" not in capsys.readouterr().err

    def test_fork_flag_refuses_a_non_ancestor_and_writes_no_config(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        repo.git("checkout", "-b", "b")
        repo.commit("b1.txt", "b1", "B1")
        repo.checkout("main")
        off_line = repo.commit("m.txt", "m", "M")

        monkeypatch.chdir(repo.worktree("b", str(tmp_path / "wt-b")))
        with pytest.raises(TreeError) as exc:
            cmd_attach(_ns(parent="main", fork=off_line))

        assert exc.value.code == 4
        assert repo.git("config", "--get", "branch.b.tree-parent-branch", check=False) == ""
        assert repo.git("config", "--get", "branch.b.tree-fork-commit", check=False) == ""


class TestDetach:
    def _branch_config(self, repo: RepoHelper, branch: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "config", f"branch.{branch}.tree-parent-branch"],
            cwd=repo.work,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_removes_tree_parent(self, repo: RepoHelper, monkeypatch) -> None:
        repo.branch("feature", parent="main")
        repo.checkout("feature")
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_detach(_ns())
        assert self._branch_config(repo, "feature").returncode != 0

    def test_yes_skips_confirmation(self, repo: RepoHelper, monkeypatch) -> None:
        repo.branch("feature", parent="main")
        repo.checkout("main")

        def _no_confirm(_message: str) -> bool:
            raise AssertionError("confirm should not be consulted with --yes")

        monkeypatch.setattr("builtins.input", _no_confirm)
        cmd_detach(_ns(branch="feature", yes=True))

        assert self._branch_config(repo, "feature").returncode != 0

    def test_declined_confirmation_keeps_parent(self, repo: RepoHelper, monkeypatch) -> None:
        repo.branch("feature", parent="main")
        repo.checkout("feature")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        cmd_detach(_ns())
        # Config untouched: still attached to main.
        result = self._branch_config(repo, "feature")
        assert result.returncode == 0
        assert result.stdout.strip() == "main"

    def test_detach_with_children_lists_remaining_trees(
        self, repo: RepoHelper, capsys, monkeypatch
    ) -> None:
        # mid is a tree-child of main and parent of leaf. Detaching mid splits the forest:
        # mid+leaf become their own tree, and main's tree (now without mid) is "remaining".
        repo.branch("topic", parent="main")
        repo.branch("mid", parent="main")
        repo.branch("leaf", parent="mid")
        repo.checkout("mid")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_detach(_ns())

        # Forest actually split: mid is now a root carrying leaf; main keeps topic.
        graph = discover()
        assert "mid" not in graph.parent_of
        assert graph.parent_of["leaf"] == "mid"
        assert graph.parent_of["topic"] == "main"
        assert roots(graph) == ["main", "mid"]

        out = capsys.readouterr().out
        assert "they will form a separate tree" in out
        assert "leaf" in out
        assert "Remaining tree(s):" in out
        assert "topic" in out  # still under main

    def test_detach_breaks_manual_cycle(self, repo: RepoHelper, monkeypatch, capsys) -> None:
        # A hand-edited config can hold a cycle. discover() now warns and prunes the cyclic
        # edges rather than raising, so detach runs its normal graph path and still unsets
        # the config, breaking the cycle.
        repo.git("branch", "a")
        repo.git("branch", "b")
        repo.set_parent("a", "b")
        repo.set_parent("b", "a")  # a <-> b cycle
        monkeypatch.setattr("builtins.input", lambda _: "y")

        cmd_detach(_ns(branch="a"))

        captured = capsys.readouterr()
        assert "cycle" in captured.err  # discover() warned about the cycle
        assert "Detached a (was child of b)" in captured.out

        assert self._branch_config(repo, "a").returncode != 0  # a's tree config is unset
        graph = discover()  # no longer raises; the cycle is broken
        assert "a" not in graph.parent_of
        assert graph.parent_of["b"] == "a"

    def test_detach_not_in_tree_exits(self, repo: RepoHelper) -> None:
        repo.git("branch", "orphan")
        repo.checkout("orphan")

        with pytest.raises(SystemExit):
            cmd_detach(_ns())


class TestDetachRemoteAnchor:
    def test_detached_subtree_keeps_a_pushable_remote(self, repo: RepoHelper, tmp_path) -> None:
        """A tree's remote lives on its root, so a new root needs one carried over.

        `rebase` and `split` already do this when they re-root a subtree; `detach` did not, so
        the detached subtree had no remote and the next `push` refused with a config hint.
        """
        repo.branch("A", parent="main")
        repo.worktree("A", str(tmp_path / "wt-A"))
        repo.branch("B", parent="A")
        repo.worktree("B", str(tmp_path / "wt-B"))
        repo.git("config", "branch.main.remote", "origin")

        cmd_detach(cli_args(branch="A", yes=True))

        assert repo.git("config", "--get", "branch.A.remote", check=False) == "origin"
        assert repo.git("config", "--get", "branch.main.remote", check=False) == "origin"
