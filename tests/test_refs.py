"""Ref resolution edge cases: detached HEAD and structural root discovery."""

from __future__ import annotations

import pytest

from git_tree._errors import TreeError
from git_tree._git import current_branch
from git_tree.cli import discover, root_of, roots

from .conftest import RepoHelper


class TestCurrentBranch:
    def test_rejects_detached_head(self, repo: RepoHelper) -> None:
        assert current_branch() == "main"
        repo.git("checkout", "--detach")
        with pytest.raises(TreeError):
            current_branch()


class TestRoots:
    def test_discovers_every_root_in_a_forest(self, repo: RepoHelper) -> None:
        # One stack rooted at main, a second rooted at an unregistered base.
        repo.branch("topic", parent="main")
        repo.git("branch", "release")  # real branch, no tree-parent -> a second root
        repo.branch("feat", parent="release")

        graph = discover()
        assert roots(graph) == ["main", "release"]
        # Assert both directions so a helper that always returns the first root can't pass.
        assert root_of(graph, "topic") == "main"
        assert root_of(graph, "feat") == "release"

    def test_works_with_non_main_trunk(self, repo: RepoHelper) -> None:
        # Repo whose only root is `trunk`, with no `main` ref present at all.
        repo.git("branch", "-m", "main", "trunk")
        repo.branch("feat", parent="trunk")

        graph = discover()
        assert roots(graph) == ["trunk"]
        assert root_of(graph, "feat") == "trunk"
