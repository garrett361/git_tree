from __future__ import annotations

import argparse

import pytest

from git_tree.cli import TreeError, cmd_branch, discover

from .conftest import RepoHelper, _git


def _ns(command: str = "branch", **kwargs: str) -> object:
    return argparse.Namespace(command=command, **kwargs)


class TestBranch:
    def test_creates_branch_with_parent_config(self, repo: RepoHelper, tmp_path) -> None:
        cmd_branch(_ns(name="child", path=str(tmp_path / "wt-child")))
        graph = discover()
        assert graph.parent_of["child"] == "main"

    def test_branch_starts_at_current_head(self, repo: RepoHelper, tmp_path) -> None:
        head_before = repo.head
        cmd_branch(_ns(name="child", path=str(tmp_path / "wt-child")))
        child_tip = repo.git("rev-parse", "child")
        assert child_tip == head_before

    def test_creates_worktree(self, repo: RepoHelper, tmp_path) -> None:
        wt_path = str(tmp_path / "wt-child")
        cmd_branch(_ns(name="child", path=wt_path))
        result = repo.git("worktree", "list", "--porcelain")
        assert "child" in result

    def test_worktree_add_failure_raises(self, repo: RepoHelper, capsys, tmp_path) -> None:
        # A path that already exists as a file makes `git worktree add` fail; cmd_branch
        # must surface that as a clear error and not register a half-created branch.
        bad_path = tmp_path / "exists"
        bad_path.write_text("not a directory")

        with pytest.raises(TreeError):
            cmd_branch(_ns(name="child", path=str(bad_path)))

        assert "failed to create worktree" in capsys.readouterr().err
        assert "child" not in discover().parent_of


class TestBranchExisting:
    def test_adopts_existing_untracked_branch(self, repo: RepoHelper, tmp_path) -> None:
        repo.git("branch", "feature")  # exists on main, not in the tree
        cmd_branch(_ns(name="feature", path=str(tmp_path / "wt-feature")))
        graph = discover()
        assert graph.parent_of["feature"] == "main"
        assert "branch refs/heads/feature" in repo.git("worktree", "list", "--porcelain")
        assert repo.git("config", "branch.feature.tree-fork-commit") == repo.git(
            "merge-base", "main", "feature"
        )

    def test_already_tracked_branch_raises_keeps_parent(self, repo: RepoHelper, tmp_path) -> None:
        repo.branch("base", parent="main")  # tracked under main
        repo.git("branch", "other")
        repo.checkout("other")  # a wrongful re-parent would point base at `other`
        target = tmp_path / "wt-base"
        with pytest.raises(TreeError):
            cmd_branch(_ns(name="base", path=str(target)))
        assert discover().parent_of["base"] == "main"  # parent unchanged
        assert not target.exists()  # no worktree created

    def test_root_with_child_raises(self, repo: RepoHelper, tmp_path) -> None:
        repo.git("branch", "rootb")  # rootb has no tree-parent ...
        repo.branch("childb", parent="rootb")  # ... but is childb's parent, so it's in the tree
        target = tmp_path / "wt-rootb"
        with pytest.raises(TreeError):
            cmd_branch(_ns(name="rootb", path=str(target)))
        graph = discover()
        assert "rootb" not in graph.parent_of
        assert graph.parent_of["childb"] == "rootb"
        assert not target.exists()

    def test_adopts_non_descendant_with_warning(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.git("branch", "feature")
        repo.checkout("feature")
        repo.commit("x.txt", "x", "diverge on feature")
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")
        # On main; feature shares only the initial commit with main's tip.
        cmd_branch(_ns(name="feature", path=str(tmp_path / "wt-feature")))
        assert discover().parent_of["feature"] == "main"
        assert "does not appear to descend" in capsys.readouterr().err
        fork = repo.git("config", "branch.feature.tree-fork-commit")
        assert fork == repo.git("merge-base", "main", "feature")
        assert fork != repo.git("rev-parse", "main")  # the merge-base, not the parent tip

    def test_no_common_history_raises_no_worktree(self, repo: RepoHelper, capsys, tmp_path) -> None:
        orphan_wt = tmp_path / "orphan-wt"
        repo.git("worktree", "add", "--detach", str(orphan_wt))
        _git("checkout", "--orphan", "orphan", cwd=orphan_wt)
        (orphan_wt / "o.txt").write_text("orphan")
        _git("add", "o.txt", cwd=orphan_wt)
        _git("commit", "-m", "orphan root", cwd=orphan_wt)
        repo.git("worktree", "remove", str(orphan_wt))

        target = tmp_path / "wt-orphan"
        with pytest.raises(TreeError):
            cmd_branch(_ns(name="orphan", path=str(target)))
        assert "No common history" in capsys.readouterr().err
        assert not target.exists()  # validated before the worktree is created

    def test_self_branch_raises(self, repo: RepoHelper, tmp_path) -> None:
        target = tmp_path / "wt-main"
        with pytest.raises(TreeError):
            cmd_branch(_ns(name="main", path=str(target)))
        assert "main" not in discover().parent_of
        assert not target.exists()

    def test_adopt_branch_checked_out_elsewhere_raises_unregistered(
        self, repo: RepoHelper, tmp_path
    ) -> None:
        # A branch already checked out in another worktree can't get a second worktree;
        # the failed `worktree add` must leave no tree-config edge behind (it runs before
        # _register_child).
        repo.git("branch", "feature")
        repo.worktree("feature")  # feature now checked out in its own worktree
        target = tmp_path / "second-feature-wt"
        with pytest.raises(TreeError):
            cmd_branch(_ns(name="feature", path=str(target)))
        assert "feature" not in discover().parent_of
        assert not target.exists()

    def test_tag_name_creates_new_branch(self, repo: RepoHelper, tmp_path) -> None:
        repo.git("tag", "v1")  # a tag, not a branch — must not hijack the adopt path
        cmd_branch(_ns(name="v1", path=str(tmp_path / "wt-v1")))
        # Took the new-branch path: a real branch refs/heads/v1 was created at main's tip
        # and registered under main. (discover() can't be used here — a same-named tag and
        # branch make %(refname:short) ambiguous, a separate git quirk.)
        assert repo.git("rev-parse", "refs/heads/v1") == repo.git("rev-parse", "main")
        assert repo.git("config", "branch.v1.tree-parent-branch") == "main"
