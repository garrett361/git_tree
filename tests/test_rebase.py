from __future__ import annotations

import argparse

import pytest

from git_tree.cli import TreeError, _root_remote, cmd_push, cmd_rebase, discover

from .conftest import RepoHelper


def _ns(target: str, yes: bool = False) -> object:
    return argparse.Namespace(
        command="rebase", target=target, dry_run=False, no_auto_rerere=False, yes=yes
    )


def _no_confirm(_message: str) -> bool:
    raise AssertionError("confirm should not be consulted with --yes")


class TestRebase:
    def test_yes_skips_confirmation(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)
        monkeypatch.chdir(wt)

        monkeypatch.setattr("git_tree.cli.confirm", _no_confirm)
        cmd_rebase(_ns(target="main", yes=True))

        log = repo.git("log", "--oneline", "feature")
        assert "advance main" in log
        assert "feature commit" in log

    def test_rebases_onto_target(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))

        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")

        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        log = repo.git("log", "--oneline", "feature")
        assert "advance main" in log
        assert "feature commit" in log

    def test_updates_tree_parent_config(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.git("branch", "base")
        repo.set_parent("base", "main")
        repo.worktree("base", str(tmp_path / "wt-base"))
        repo.branch("child", parent="base")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt)
        repo.git("commit", "-m", "child commit", cwd=wt)

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        graph = discover()
        assert graph.parent_of["child"] == "main"

    def test_cascades_to_descendants(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "b commit", cwd=wt_b)
        repo.branch("c", parent="b")
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        (wt_c / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt_c)
        repo.git("commit", "-m", "c commit", cwd=wt_c)

        repo.checkout("main")
        repo.commit("m2.txt", "m2", "new main commit")

        monkeypatch.chdir(wt_b)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        c_log = repo.git("log", "--oneline", "c")
        assert "new main commit" in c_log
        assert "c commit" in c_log

    def test_excludes_old_parent_commits(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        """After squash-merge, rebase replays only child's unique commits, not parent's."""
        repo.branch("parent-branch", parent="main")
        wt_p = repo.worktree("parent-branch", str(tmp_path / "wt-parent"))
        (wt_p / "p1.txt").write_text("p1")
        repo.git("add", "p1.txt", cwd=wt_p)
        repo.git("commit", "-m", "parent commit 1", cwd=wt_p)
        (wt_p / "p2.txt").write_text("p2")
        repo.git("add", "p2.txt", cwd=wt_p)
        repo.git("commit", "-m", "parent commit 2", cwd=wt_p)

        repo.branch("child-branch", parent="parent-branch")
        wt_c = repo.worktree("child-branch", str(tmp_path / "wt-child"))
        (wt_c / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt_c)
        repo.git("commit", "-m", "child unique commit", cwd=wt_c)

        # Simulate squash-merge of parent-branch into main
        repo.checkout("main")
        repo.git("merge", "--squash", "parent-branch")
        repo.git("commit", "-m", "squash merge of parent-branch")

        monkeypatch.chdir(wt_c)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        log = repo.git("log", "--oneline", "child-branch")
        assert "child unique commit" in log
        assert "squash merge" in log
        assert "parent commit 1" not in log
        assert "parent commit 2" not in log

    def test_confirmation_decline_aborts(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)
        head_before = repo.git("rev-parse", "feature")

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        cmd_rebase(_ns(target="main"))

        assert repo.git("rev-parse", "feature") == head_before

    def test_dry_does_not_modify(self, repo: RepoHelper, capsys, tmp_path, monkeypatch) -> None:
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)
        head_before = repo.git("rev-parse", "feature")

        monkeypatch.chdir(wt)
        cmd_rebase(
            argparse.Namespace(command="rebase", target="main", dry_run=True, no_auto_rerere=False)
        )

        assert repo.git("rev-parse", "feature") == head_before
        out = capsys.readouterr().out
        assert "Rebasing onto" in out

    def test_conflict_aborts(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.commit("shared.txt", "original", "base")
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "shared.txt").write_text("feature version")
        repo.git("add", "shared.txt", cwd=wt)
        repo.git("commit", "-m", "feature modifies shared", cwd=wt)

        repo.checkout("main")
        repo.commit("shared.txt", "main version", "main modifies shared")

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")

        with pytest.raises(SystemExit):
            cmd_rebase(_ns(target="main"))

    def test_rebase_works_in_main_worktree(self, repo: RepoHelper, monkeypatch) -> None:
        """Branch checked out in main worktree (no secondary worktree) can be rebased."""
        repo.commit("a1.txt", "a1", "advance main")
        repo.branch("feature", parent="main")
        repo.checkout("feature")
        repo.commit("f1.txt", "f1", "on feature")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        log = repo.git("log", "--oneline", "feature")
        assert "on feature" in log

    def test_rebase_propagates_to_one_child_not_sibling(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """main -> dev1, dev2: rebasing dev1 onto an advanced main updates dev1 (and its
        descendants) but leaves the sibling dev2 untouched."""
        repo.branch("dev1", parent="main")
        wt1 = repo.worktree("dev1", str(tmp_path / "wt-dev1"))
        (wt1 / "d1.txt").write_text("d1")
        repo.git("add", "d1.txt", cwd=wt1)
        repo.git("commit", "-m", "dev1 commit", cwd=wt1)

        repo.branch("dev2", parent="main")
        wt2 = repo.worktree("dev2", str(tmp_path / "wt-dev2"))
        (wt2 / "d2.txt").write_text("d2")
        repo.git("add", "d2.txt", cwd=wt2)
        repo.git("commit", "-m", "dev2 commit", cwd=wt2)

        # Bring new upstream work into main.
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "new main commit")

        # Propagate main into dev1 only, via rebase from dev1's worktree.
        monkeypatch.chdir(wt1)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        dev1_log = repo.git("log", "--oneline", "dev1")
        assert "new main commit" in dev1_log
        assert "dev1 commit" in dev1_log

        dev2_log = repo.git("log", "--oneline", "dev2")
        assert "new main commit" not in dev2_log  # sibling not propagated
        assert "dev2 commit" in dev2_log

    def test_rebase_diverged_parent_preserves_commits(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """When the parent advanced without propagate, rebase preserves the child's commits."""
        repo.commit("a1.txt", "a1", "base for feature")
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "first on feature", cwd=wt)
        (wt / "f2.txt").write_text("f2")
        repo.git("add", "f2.txt", cwd=wt)
        repo.git("commit", "-m", "second on feature", cwd=wt)

        # Advance main (parent diverges from feature's fork point)
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "advance main past fork")

        # Create a new target branch
        repo.git("branch", "new-base")

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="new-base"))

        log = repo.git("log", "--oneline", "feature")
        assert "first on feature" in log
        assert "second on feature" in log

    def test_rebase_onto_descendant_raises_before_side_effects(
        self, repo: RepoHelper, monkeypatch
    ) -> None:
        # main <- a <- b; rebasing a onto its own descendant b would loop. The guard
        # must fire before any rebase: a's tip stays put and its parent stays main.
        repo.branch("a", parent="main")
        repo.branch("b", parent="a")
        repo.checkout("a")
        tip_before = repo.git("rev-parse", "a")

        # input must never be consulted — the guard aborts before the confirm prompt.
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("reached confirm"))
        with pytest.raises(TreeError):
            cmd_rebase(_ns(target="b"))

        assert repo.git("rev-parse", "a") == tip_before
        assert discover().parent_of["a"] == "main"

    def test_rebase_nonexistent_target_raises_before_side_effects(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        # A typo'd target must be rejected before the confirm prompt and before any
        # rebase: the branch tip and its tree-parent stay put.
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)
        tip_before = repo.git("rev-parse", "feature")

        monkeypatch.chdir(wt)
        # input must never be consulted — the guard aborts before the confirm prompt.
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("reached confirm"))
        with pytest.raises(TreeError):
            cmd_rebase(_ns(target="no-such-branch"))

        assert "Rebase target no-such-branch does not exist" in capsys.readouterr().err
        assert repo.git("rev-parse", "feature") == tip_before
        assert discover().parent_of["feature"] == "main"

    def test_rebase_onto_self_raises(self, repo: RepoHelper, monkeypatch) -> None:
        repo.branch("a", parent="main")
        repo.checkout("a")
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("reached confirm"))
        with pytest.raises(TreeError):
            cmd_rebase(_ns(target="a"))
        assert discover().parent_of["a"] == "main"

    def test_rebase_out_of_tree_carries_remote_and_push_resolves(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        # main(root, remote=origin) <- feature. Rebasing feature onto a fresh out-of-tree
        # branch re-roots the tree; the remote must follow so push still resolves.
        repo.git("config", "branch.main.remote", "origin")
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)
        repo.git("branch", "new-base")  # not in the tree, no remote

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="new-base"))

        assert _root_remote(discover(), "feature") == ("new-base", "origin")
        # The actual symptom of the bug: push could not resolve a remote. Now it can.
        capsys.readouterr()
        cmd_push(argparse.Namespace(dry_run=True))
        assert "Pushing to origin" in capsys.readouterr().out

    def test_rebase_within_tree_leaves_remote_untouched(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        # main(root, remote=origin) <- base <- child. Rebasing child onto main stays in
        # the same tree (root unchanged), so no remote should be created or moved.
        repo.git("config", "branch.main.remote", "origin")
        repo.git("branch", "base")
        repo.set_parent("base", "main")
        repo.worktree("base", str(tmp_path / "wt-base"))
        repo.branch("child", parent="base")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt)
        repo.git("commit", "-m", "child commit", cwd=wt)

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        assert repo.git("config", "branch.main.remote") == "origin"
        assert repo.git("config", "branch.child.remote", check=False) == ""
        assert repo.git("config", "branch.base.remote", check=False) == ""

    def test_rebase_onto_foreign_tree_keeps_its_remote(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        # feature is under main(remote=origin); `other` is a separate root with its own
        # remote. Rebasing feature onto other must NOT overwrite other's remote; feature
        # adopts other's tree remote via resolution.
        repo.git("config", "branch.main.remote", "origin")
        repo.git("branch", "other")
        repo.git("config", "branch.other.remote", "upstream")
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="other"))

        assert repo.git("config", "branch.other.remote") == "upstream"
        assert _root_remote(discover(), "feature") == ("other", "upstream")

    def test_rebase_out_of_tree_keeps_old_root_remote_for_siblings(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        # main(root, remote=origin) roots both `a` and `sibling`. Rebasing `a` out to a
        # new base must COPY, not move: main keeps its remote so `sibling`, still rooted
        # there, continues to resolve it.
        repo.git("config", "branch.main.remote", "origin")
        repo.branch("a", parent="main")
        wt = repo.worktree("a", str(tmp_path / "wt-a"))
        (wt / "a1.txt").write_text("a1")
        repo.git("add", "a1.txt", cwd=wt)
        repo.git("commit", "-m", "a commit", cwd=wt)
        repo.branch("sibling", parent="main")
        repo.git("branch", "new-base")  # out-of-tree target

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="new-base"))

        assert repo.git("config", "branch.main.remote") == "origin"
        assert _root_remote(discover(), "sibling") == ("main", "origin")
