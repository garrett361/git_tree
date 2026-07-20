from __future__ import annotations

import argparse

from git_tree.cli import (
    BranchInfo,
    _git_status_summary,
    _worktree_status,
    cmd_tree,
    discover,
    format_tree,
    main,
)

from .conftest import RepoHelper


class TestFormatTree:
    def test_empty_repo_prints_registration_hint(self, repo: RepoHelper, capsys) -> None:
        cmd_tree(argparse.Namespace())
        out = capsys.readouterr().out
        assert "no tree-branches registered" in out

    def test_single_child(self, repo: RepoHelper) -> None:
        repo.branch("feature", parent="main")
        graph = discover()
        output = format_tree(graph, root="main")
        assert "└── feature" in output

    def test_multiple_children(self, repo: RepoHelper) -> None:
        repo.branch("a", parent="main")
        repo.branch("b", parent="main")
        graph = discover()
        output = format_tree(graph, root="main")
        lines = output.splitlines()
        assert lines[0] == "main"
        connectors = [line.strip()[:3] for line in lines[1:]]
        assert "├──" in connectors or "└──" in connectors

    def test_deep_chain(self, repo: RepoHelper) -> None:
        repo.branch("b", parent="main")
        repo.branch("c", parent="b")
        graph = discover()
        output = format_tree(graph, root="main")
        assert "main" in output
        assert "b" in output
        assert "c" in output

    def test_custom_root(self, repo: RepoHelper) -> None:
        repo.branch("b", parent="main")
        repo.branch("c", parent="b")
        graph = discover()
        output = format_tree(graph, root="b")
        lines = output.splitlines()
        assert lines[0] == "b"
        assert "c" in output

    def test_no_worktree_annotation(self, repo: RepoHelper) -> None:
        repo.branch("feature", parent="main")
        graph = discover()
        output = format_tree(graph, root="main")
        assert "(no worktree)" in output


class TestCmdTreeForest:
    def test_all_renders_every_root(self, repo: RepoHelper, capsys) -> None:
        # A stack rooted at main, plus a separate forest whose base branch has no
        # tree-parent (so it isn't reachable from main). `--all` shows both.
        repo.branch("topic", parent="main")
        repo.git("branch", "standalone")  # real branch, not registered in the tree
        repo.branch("leaf", parent="standalone")

        cmd_tree(argparse.Namespace(all=True))
        out = capsys.readouterr().out

        assert "topic" in out
        # A root whose base isn't main — only visible with --all.
        assert "standalone" in out
        assert "leaf" in out

    def test_default_shows_only_current_tree(self, repo: RepoHelper, capsys) -> None:
        # Two trees; standing in the `standalone` tree, the default view shows only it.
        repo.branch("topic", parent="main")  # main's tree
        repo.git("branch", "standalone")
        repo.branch("leaf", parent="standalone")  # standalone's tree
        repo.checkout("leaf")

        cmd_tree(argparse.Namespace())
        out = capsys.readouterr().out

        assert "standalone" in out
        assert "leaf" in out
        assert "topic" not in out  # main's tree is not the current tree

    def test_default_off_tree_points_to_all(self, repo: RepoHelper, capsys) -> None:
        # On a branch in no tree while other trees exist: don't dump everything.
        repo.git("branch", "standalone")
        repo.branch("leaf", parent="standalone")
        repo.checkout("main")  # main has no tree-parent and no children -> not a tree-branch

        cmd_tree(argparse.Namespace())
        out = capsys.readouterr().out

        assert "Not on a tree-branch" in out
        assert "--all" in out
        assert "leaf" not in out

    def test_all_shows_trees_even_when_off_tree(self, repo: RepoHelper, capsys) -> None:
        # --all shows every tree regardless of the current branch (even off a tree).
        repo.git("branch", "standalone")
        repo.branch("leaf", parent="standalone")
        repo.checkout("main")  # not a tree-branch (no parent, no children)

        cmd_tree(argparse.Namespace(all=True))
        out = capsys.readouterr().out
        assert "standalone" in out
        assert "leaf" in out

    def test_all_flag_parses_via_cli(self, repo: RepoHelper, capsys) -> None:
        repo.branch("topic", parent="main")
        repo.git("branch", "standalone")
        repo.branch("leaf", parent="standalone")

        main(["--all"])  # `git tree --all`
        out = capsys.readouterr().out
        assert "topic" in out
        assert "standalone" in out


class TestStatusSummary:
    def test_counts_staged_type_change(self, repo: RepoHelper, tmp_path) -> None:
        repo.branch("feat", parent="main")
        wt = repo.worktree("feat", str(tmp_path / "wt-feat"))
        (wt / "f").write_text("content")
        repo.git("add", "f", cwd=wt)
        repo.git("commit", "-m", "add f", cwd=wt)

        # Replace the regular file with a symlink and stage it -> "T " (type-change).
        (wt / "f").unlink()
        (wt / "f").symlink_to("target")
        repo.git("add", "f", cwd=wt)

        summary = _git_status_summary("feat", BranchInfo(name="feat", worktree=wt), remote=None)
        assert "+1" in summary  # counted as staged (T was previously ignored)


class TestWorktreeStatus:
    def test_tallies_staged_modified_untracked(self, repo: RepoHelper, tmp_path) -> None:
        repo.branch("feat", parent="main")
        wt = repo.worktree("feat", str(tmp_path / "wt-feat"))
        (wt / "t").write_text("tracked")
        repo.git("add", "t", cwd=wt)
        repo.git("commit", "-m", "add t", cwd=wt)

        (wt / "s").write_text("staged new")
        repo.git("add", "s", cwd=wt)  # "A " -> staged
        (wt / "t").write_text("changed")  # " M" -> modified (unstaged)
        (wt / "u").write_text("untracked")  # "??" -> untracked

        status = _worktree_status(wt)
        assert (status.staged, status.modified, status.untracked, status.conflicted) == (1, 1, 1, 0)
        assert status.dirty is True

    def test_clean_worktree_is_not_dirty(self, repo: RepoHelper, tmp_path) -> None:
        repo.branch("feat", parent="main")
        wt = repo.worktree("feat", str(tmp_path / "wt-feat"))
        status = _worktree_status(wt)
        assert (status.staged, status.modified, status.untracked, status.conflicted) == (0, 0, 0, 0)
        assert status.dirty is False


class TestStatusRemote:
    def test_status_uses_root_remote(self, repo: RepoHelper, capsys, tmp_path) -> None:
        # The tree's root points at a second remote; ahead/behind is computed against it.
        upstream = tmp_path / "upstream.git"
        upstream.mkdir()
        repo.git("init", "--bare", cwd=upstream)
        repo.git("remote", "add", "upstream", str(upstream))
        repo.git("config", "branch.main.remote", "upstream")

        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt)
        repo.git("commit", "-m", "c1", cwd=wt)
        repo.git("push", "-u", "upstream", "child", cwd=wt)  # upstream/child now exists
        (wt / "c2.txt").write_text("c2")
        repo.git("add", "c2.txt", cwd=wt)
        repo.git("commit", "-m", "c2", cwd=wt)  # 1 ahead of upstream/child

        cmd_tree(argparse.Namespace())
        out = capsys.readouterr().out
        assert "⇡1" in out  # ahead computed against upstream/child, not origin

    def test_status_no_root_remote_is_graceful(self, repo: RepoHelper, capsys, tmp_path) -> None:
        # Root has no remote: render must not error and must show no ahead/behind markers.
        repo.git("config", "--unset", "branch.main.remote")
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt)
        repo.git("commit", "-m", "c1", cwd=wt)

        cmd_tree(argparse.Namespace())
        out = capsys.readouterr().out
        assert "child" in out
        assert "⇡" not in out
        assert "⇣" not in out
