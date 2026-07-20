from __future__ import annotations

from git_tree.cli import discover

from .conftest import RepoHelper


class TestDiscover:
    def test_reads_tree_parent_config(self, repo: RepoHelper) -> None:
        repo.branch("feature-a", parent="main")
        graph = discover()
        assert graph.parent_of["feature-a"] == "main"

    def test_branches_without_config_excluded(self, repo: RepoHelper) -> None:
        repo.git("branch", "no-config")
        graph = discover()
        assert "no-config" not in graph.parent_of

    def test_children_of_populated(self, repo: RepoHelper) -> None:
        repo.branch("child1", parent="main")
        repo.branch("child2", parent="main")
        graph = discover()
        assert set(graph.children_of["main"]) == {"child1", "child2"}

    def test_linear_chain(self, repo: RepoHelper) -> None:
        repo.branch("b", parent="main")
        repo.branch("c", parent="b")
        graph = discover()
        assert graph.parent_of["b"] == "main"
        assert graph.parent_of["c"] == "b"
        assert graph.children_of["b"] == ["c"]

    def test_deleted_parent_skipped_with_warning(self, repo: RepoHelper, capsys) -> None:
        """Branch whose tree-parent was deleted should be excluded, not crash."""
        repo.branch("child", parent="main")
        repo.branch("grandchild", parent="child")
        repo.git("branch", "-D", "child")

        graph = discover()
        assert "child" not in graph.parent_of
        assert "grandchild" not in graph.parent_of
        assert "grandchild" not in graph.branches
        err = capsys.readouterr().err
        assert "grandchild" in err
        assert "child" in err


class TestDownstream:
    def test_linear_descendants(self, repo: RepoHelper) -> None:
        repo.branch("b", parent="main")
        repo.branch("c", parent="b")
        repo.branch("d", parent="c")
        graph = discover()
        assert graph.downstream_from("main") == ["b", "c", "d"]

    def test_fork_descendants(self, repo: RepoHelper) -> None:
        repo.branch("b1", parent="main")
        repo.branch("b2", parent="main")
        repo.branch("c", parent="b1")
        graph = discover()
        desc = graph.downstream_from("main")
        assert "b1" in desc
        assert "b2" in desc
        assert "c" in desc
        assert desc.index("b1") < desc.index("c")

    def test_no_descendants(self, repo: RepoHelper) -> None:
        repo.branch("leaf", parent="main")
        graph = discover()
        assert graph.downstream_from("leaf") == []

    def test_does_not_include_root(self, repo: RepoHelper) -> None:
        repo.branch("b", parent="main")
        graph = discover()
        assert "main" not in graph.downstream_from("main")
