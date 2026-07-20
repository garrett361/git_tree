from __future__ import annotations

import pytest

from git_tree.cli import discover, main, roots

from .conftest import RepoHelper


class TestEdgeCases:
    def test_missing_parent_branch_excluded(self, repo: RepoHelper, capsys) -> None:
        repo.git("config", "branch.main.tree-parent-branch", "nonexistent")
        graph = discover()
        assert "main" not in graph.parent_of
        assert "main" not in graph.branches
        err = capsys.readouterr().err
        assert "nonexistent" in err

    def test_empty_graph(self, repo: RepoHelper) -> None:
        graph = discover()
        assert graph.parent_of == {}
        assert graph.children_of == {}


class TestGitFailureSurfacesCleanly:
    def test_outside_repo_reports_git_error_not_traceback(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        # Outside any git repo the first git() in discover() exits 128. main() must surface
        # git's own message as a clean error, not a raw CalledProcessError traceback.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # don't find a parent repo

        with pytest.raises(SystemExit):
            main(["remove", "whatever"])

        err = capsys.readouterr().err
        assert "git command failed" in err
        assert "git worktree list --porcelain" in err
        assert "fatal" in err  # git's own stderr is included


class TestCycles:
    def test_cycle_warns_and_prunes(self, repo: RepoHelper, capsys) -> None:
        repo.git("branch", "a")
        repo.git("branch", "b")
        repo.set_parent("a", "b")
        repo.set_parent("b", "a")

        graph = discover()  # warns and prunes rather than raising
        assert "cycle" in capsys.readouterr().err
        # A pure 2-cycle: both nodes lose their edges and are absent from parent_of and
        # children_of, so neither shows up as a root either.
        assert "a" not in graph.parent_of
        assert "b" not in graph.parent_of
        assert graph.children_of == {}

    def test_self_parent_warns_and_prunes(self, repo: RepoHelper, capsys) -> None:
        repo.git("branch", "a")
        repo.set_parent("a", "a")

        graph = discover()
        assert "cycle" in capsys.readouterr().err
        assert "a" not in graph.parent_of
        assert "a" not in graph.children_of

    def test_cycle_node_with_external_child_stays_its_root(self, repo: RepoHelper, capsys) -> None:
        # A cyclic node that also has a non-cyclic child keeps that child: only the cyclic
        # links are dropped, so the node remains a root carrying its healthy descendant.
        repo.git("branch", "a")
        repo.git("branch", "b")
        repo.git("branch", "c")
        repo.set_parent("a", "b")
        repo.set_parent("b", "a")  # a <-> b cycle
        repo.set_parent("c", "b")  # healthy external child of b

        graph = discover()
        assert "cycle" in capsys.readouterr().err
        assert graph.parent_of["c"] == "b"  # external child kept attached
        assert "a" not in graph.parent_of  # cyclic links dropped
        assert "b" not in graph.parent_of
        assert "b" in roots(graph)  # b remains a root carrying c

    def test_unrelated_cycle_does_not_block_healthy_tree(self, repo: RepoHelper, capsys) -> None:
        repo.branch("feat", parent="main")  # healthy: main -> feat
        repo.git("branch", "x")
        repo.git("branch", "y")
        repo.set_parent("x", "y")  # unrelated x <-> y cycle
        repo.set_parent("y", "x")

        graph = discover()
        assert "cycle" in capsys.readouterr().err
        # Healthy tree is intact and traversable.
        assert graph.parent_of["feat"] == "main"
        assert graph.children_of["main"] == ["feat"]
        assert graph.downstream_from("main") == ["feat"]
        # Cyclic branches pruned.
        assert "x" not in graph.parent_of
        assert "y" not in graph.parent_of
