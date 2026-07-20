from __future__ import annotations

import argparse

import pytest

from git_tree.cli import TreeError, cmd_remove, discover

from .conftest import RepoHelper


def _ns(branch: str | None = None, yes: bool = False) -> object:
    return argparse.Namespace(branch=branch, yes=yes)


def _branch_exists(repo: RepoHelper, name: str) -> bool:
    return repo.git("rev-parse", "--verify", "--quiet", name, check=False) != ""


def _no_confirm(_message: str) -> bool:
    raise AssertionError("confirm should not be consulted with --yes")


class TestRemove:
    def test_yes_skips_confirmation(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))
        (wt / "a.txt").write_text("a")
        repo.git("add", "a.txt", cwd=wt)
        repo.git("commit", "-m", "a work", cwd=wt)

        monkeypatch.setattr("git_tree.cli.confirm", _no_confirm)
        cmd_remove(_ns("A", yes=True))

        assert not wt.exists()
        assert "A" not in discover().parent_of

    def test_removes_worktree_keeps_branch_and_detaches(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))
        (wt / "a.txt").write_text("a")
        repo.git("add", "a.txt", cwd=wt)
        repo.git("commit", "-m", "a work", cwd=wt)
        a_sha = repo.git("rev-parse", "A")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_remove(_ns("A"))

        # Worktree gone...
        assert "wt-A" not in repo.git("worktree", "list", "--porcelain")
        assert not wt.exists()
        # ...but the branch ref and its commit survive (no data lost)...
        assert _branch_exists(repo, "A")
        assert repo.git("rev-parse", "A") == a_sha
        # ...and it's unregistered from the tree.
        assert "A" not in discover().parent_of

    def test_removes_subtree_children_first(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        repo.branch("A", parent="main")
        wt_a = repo.worktree("A", str(tmp_path / "wt-A"))
        (wt_a / "a.txt").write_text("a")
        repo.git("add", "a.txt", cwd=wt_a)
        repo.git("commit", "-m", "a work", cwd=wt_a)
        repo.git("branch", "B", cwd=wt_a)  # B at A's tip
        repo.set_parent("B", "A")
        repo.worktree("B", str(tmp_path / "wt-B"))

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_remove(_ns("A"))

        # Both worktrees gone, both branches kept, both unregistered.
        worktrees = repo.git("worktree", "list", "--porcelain")
        assert "wt-A" not in worktrees and "wt-B" not in worktrees
        assert _branch_exists(repo, "A") and _branch_exists(repo, "B")
        graph = discover()
        assert "A" not in graph.parent_of and "B" not in graph.parent_of

        # Child's worktree is removed before the parent's.
        removes = [ln for ln in capsys.readouterr().out.splitlines() if "git worktree remove" in ln]
        assert len(removes) == 2
        assert "wt-B" in removes[0]
        assert "wt-A" in removes[1]

    def test_aborts_when_a_worktree_is_dirty(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        # main -> A -> B; B has uncommitted work, so the whole op is refused atomically.
        repo.branch("A", parent="main")
        wt_a = repo.worktree("A", str(tmp_path / "wt-A"))
        repo.git("branch", "B", cwd=wt_a)
        repo.set_parent("B", "A")
        wt_b = repo.worktree("B", str(tmp_path / "wt-B"))
        (wt_b / "dirty.txt").write_text("uncommitted")  # untracked file

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(TreeError):
            cmd_remove(_ns("A"))

        err = capsys.readouterr().err
        assert "uncommitted changes" in err
        assert "B" in err
        # Nothing removed: both worktrees and the tree registration are intact.
        worktrees = repo.git("worktree", "list", "--porcelain")
        assert "wt-A" in worktrees and "wt-B" in worktrees
        assert "A" in discover().parent_of

    def test_aborts_on_staged_change(self, repo: RepoHelper, monkeypatch, capsys, tmp_path) -> None:
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))
        (wt / "s.txt").write_text("staged")
        repo.git("add", "s.txt", cwd=wt)  # staged, not committed

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(TreeError):
            cmd_remove(_ns("A"))
        assert "uncommitted changes" in capsys.readouterr().err
        assert wt.exists()
        assert "A" in discover().parent_of

    def test_does_not_touch_siblings(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("A", parent="main")
        repo.worktree("A", str(tmp_path / "wt-A"))
        repo.branch("B", parent="main")  # sibling of A, outside A's subtree
        wt_b = repo.worktree("B", str(tmp_path / "wt-B"))

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_remove(_ns("A"))

        assert wt_b.exists()
        assert "wt-B" in repo.git("worktree", "list", "--porcelain")
        assert "B" in discover().parent_of

    def test_detaches_worktreeless_branch(self, repo: RepoHelper, monkeypatch) -> None:
        repo.branch("A", parent="main")  # registered, but no worktree

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_remove(_ns("A"))

        assert _branch_exists(repo, "A")  # branch kept
        assert "A" not in discover().parent_of  # unregistered

    def test_refuses_to_remove_a_root(self, repo: RepoHelper, capsys) -> None:
        repo.branch("A", parent="main")  # main is a root
        with pytest.raises(TreeError):
            cmd_remove(_ns("main"))
        assert "not a removable tree-branch" in capsys.readouterr().err
        assert "A" in discover().parent_of  # nothing changed

    def test_refuses_to_remove_current_branch(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))
        monkeypatch.chdir(wt)  # standing on A

        with pytest.raises(TreeError):
            cmd_remove(_ns("A"))
        assert "the branch you're on" in capsys.readouterr().err
        assert wt.exists()

    def test_declined_confirmation_removes_nothing(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))

        monkeypatch.setattr("builtins.input", lambda _: "n")
        cmd_remove(_ns("A"))

        assert wt.exists()
        assert "wt-A" in repo.git("worktree", "list", "--porcelain")
        assert "A" in discover().parent_of

    def test_no_arg_picks_from_worktrees_excluding_current(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        repo.branch("A", parent="main")
        wt_a = repo.worktree("A", str(tmp_path / "wt-A"))
        repo.branch("B", parent="main")
        repo.worktree("B", str(tmp_path / "wt-B"))
        monkeypatch.chdir(wt_a)  # current branch is A -> excluded from the picker

        captured: dict[str, list[str]] = {}

        def fake_fzf(items, **kw):
            captured["items"] = items
            return ["B"]

        monkeypatch.setattr("git_tree.cli.fzf_select", fake_fzf)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_remove(_ns())  # no branch arg

        assert captured["items"] == ["B"]  # A (current) excluded
        assert "B" not in discover().parent_of
        assert "wt-B" not in repo.git("worktree", "list", "--porcelain")

    def test_no_arg_with_no_worktrees_errors(self, repo: RepoHelper, capsys) -> None:
        with pytest.raises(TreeError):
            cmd_remove(_ns())  # only main, no tree-branch worktrees
        assert "No tree-branch worktrees available" in capsys.readouterr().err

    def test_no_arg_cancel_removes_nothing(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("A", parent="main")
        repo.worktree("A", str(tmp_path / "wt-A"))
        monkeypatch.setattr("git_tree.cli.fzf_select", lambda items, **kw: [])  # cancelled

        with pytest.raises(SystemExit):
            cmd_remove(_ns())
        assert "A" in discover().parent_of  # nothing removed
