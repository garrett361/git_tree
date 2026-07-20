from __future__ import annotations

import argparse
import subprocess as sp

import pytest

from git_tree.cli import cmd_log, main

from .conftest import RepoHelper


@pytest.fixture
def capture(monkeypatch):
    captured = {}
    orig_run = sp.run

    def capture_run(cmd, **kwargs):
        kwargs["capture_output"] = True
        kwargs["text"] = True
        result = orig_run(cmd, **kwargs)
        captured["stdout"] = result.stdout
        captured["returncode"] = result.returncode
        return result

    monkeypatch.setattr("subprocess.run", capture_run)
    return captured


class TestLog:
    def test_shows_all_tree_branches(self, repo: RepoHelper, capture) -> None:
        repo.commit("a1.txt", "a1", "first on main")
        repo.branch("b", parent="main")
        repo.checkout("b")
        repo.commit("b1.txt", "b1", "on b")
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "second on main")

        with pytest.raises(SystemExit) as exc:
            cmd_log(argparse.Namespace(extra=["--no-color"]))
        assert exc.value.code == 0
        assert "on b" in capture["stdout"]
        assert "second on main" in capture["stdout"]

    def test_excludes_pre_fork_history(self, repo: RepoHelper, capture) -> None:
        repo.commit("old.txt", "old", "ancient history")
        repo.commit("a1.txt", "a1", "post-fork on main")
        repo.branch("b", parent="main")
        repo.checkout("b")
        repo.commit("b1.txt", "b1", "on b")
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "new on main")

        with pytest.raises(SystemExit):
            cmd_log(argparse.Namespace(extra=["--no-color"]))
        assert "ancient history" not in capture["stdout"]
        assert "on b" in capture["stdout"]
        assert "post-fork on main" in capture["stdout"]

    def test_no_descendants_exits_cleanly(self, repo: RepoHelper, capsys) -> None:
        # On main with nothing attached: main is not a tree-branch (no parent, no children).
        repo.commit("a1.txt", "a1", "on main")
        with pytest.raises(SystemExit) as exc:
            cmd_log(argparse.Namespace(extra=[]))
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert out.strip() == "Not on a tree-branch."

    def test_follows_current_branch_root(self, repo: RepoHelper, capture) -> None:
        # Two trees with non-overlapping messages. `release` forks before main's unique
        # commit and is its own root (it has a tree-child but no tree-parent). Standing in
        # the release tree must log the release subtree, not main's.
        repo.git("branch", "release")  # forks from main's initial commit
        repo.commit("m.txt", "m", "unique-main-commit")
        repo.checkout("release")
        repo.commit("r.txt", "r", "unique-release-commit")
        repo.branch("feat", parent="release")
        repo.checkout("feat")
        repo.commit("f.txt", "f", "unique-feat-commit")

        with pytest.raises(SystemExit) as exc:
            cmd_log(argparse.Namespace(extra=["--no-color"]))
        assert exc.value.code == 0
        out = capture["stdout"]
        assert "unique-release-commit" in out
        assert "unique-feat-commit" in out
        assert "unique-main-commit" not in out

    def test_detached_head_is_not_a_tree_branch(self, repo: RepoHelper, capsys) -> None:
        repo.git("checkout", "--detach")
        with pytest.raises(SystemExit) as exc:
            cmd_log(argparse.Namespace(extra=[]))
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == "Not on a tree-branch."

    def test_untracked_branch_is_not_a_tree_branch(self, repo: RepoHelper, capsys) -> None:
        repo.git("checkout", "-b", "scratch")  # real branch, no tree config
        with pytest.raises(SystemExit) as exc:
            cmd_log(argparse.Namespace(extra=[]))
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == "Not on a tree-branch."

    def test_flags_without_separator(self, repo: RepoHelper, capture) -> None:
        """git tree log --graph should work without requiring '--'."""
        repo.commit("a1.txt", "a1", "on main")
        repo.branch("b", parent="main")
        repo.checkout("b")
        repo.commit("b1.txt", "b1", "on b")
        repo.checkout("main")

        with pytest.raises(SystemExit) as exc:
            main(["log", "--no-color"])
        assert exc.value.code == 0

    def test_root_commit_boundary(self, repo: RepoHelper, capture) -> None:
        repo.branch("b", parent="main")
        repo.checkout("b")
        repo.commit("b1.txt", "b1", "on b")
        repo.checkout("main")

        with pytest.raises(SystemExit) as exc:
            cmd_log(argparse.Namespace(extra=["--no-color"]))
        assert exc.value.code == 0
        assert "on b" in capture["stdout"]
