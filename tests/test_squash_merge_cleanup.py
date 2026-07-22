from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from git_tree.cli import cmd_propagate, cmd_rebase, cmd_remove, discover

from .conftest import RepoHelper


def _rebase_ns(target: str) -> object:
    return argparse.Namespace(
        command="rebase", target=target, dry_run=False, no_auto_rerere=False, yes=True
    )


def _remove_ns(branch: str) -> object:
    return argparse.Namespace(branch=branch, yes=True)


def _commit(repo: RepoHelper, wt: Path, filename: str, content: str, message: str) -> None:
    (wt / filename).write_text(content)
    repo.git("add", filename, cwd=wt)
    repo.git("commit", "-m", message, cwd=wt)


class TestSquashMergeCleanup:
    """The AGENTS.md 'Squash-merge cleanup' protocol: after B is squash-merged into its
    parent, rebase each of B's children onto that parent (from the child's worktree), then
    drop the now-childless B. Uses only existing commands (rebase + continue + remove)."""

    def test_fanout_children_hoisted_then_b_removed(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """main -> B -> {C -> C2, D}. Squash-merge B into main, then follow the protocol:
        each direct child of B rebases onto main (replaying only its own commits, cascading
        to grandchildren), and the childless B is removed cleanly."""
        repo.git("branch", "B", "main")
        repo.set_parent("B", "main")
        wt_b = repo.worktree("B", str(tmp_path / "wt-B"))
        _commit(repo, wt_b, "b1.txt", "b1", "b1 commit")
        _commit(repo, wt_b, "b2.txt", "b2", "b2 commit")

        repo.git("branch", "C", "B")
        repo.set_parent("C", "B")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        _commit(repo, wt_c, "c1.txt", "c1", "c1 commit")

        repo.git("branch", "C2", "C")
        repo.set_parent("C2", "C")
        wt_c2 = repo.worktree("C2", str(tmp_path / "wt-C2"))
        _commit(repo, wt_c2, "c2.txt", "c2", "c2 grandchild commit")

        repo.git("branch", "D", "B")
        repo.set_parent("D", "B")
        wt_d = repo.worktree("D", str(tmp_path / "wt-D"))
        _commit(repo, wt_d, "d1.txt", "d1", "d1 commit")

        # Squash-merge B into main: B's work lands as one commit, not b1/b2.
        repo.checkout("main")
        repo.git("merge", "--squash", "B")
        repo.git("commit", "-m", "squash merge of B")

        # Protocol step 2: rebase each direct child of B onto main, from its own worktree.
        for child_wt in (wt_c, wt_d):
            monkeypatch.chdir(child_wt)
            cmd_rebase(_rebase_ns("main"))

        graph = discover()
        assert graph.parent_of["C"] == "main"
        assert graph.parent_of["D"] == "main"
        assert graph.parent_of["C2"] == "C"  # grandchild rides along, still under C

        for branch in ("C", "D", "C2"):
            log = repo.git("log", "--oneline", branch)
            assert "squash merge of B" in log, branch
            assert "b1 commit" not in log, branch
            assert "b2 commit" not in log, branch
        assert "c1 commit" in repo.git("log", "--oneline", "C")
        assert "d1 commit" in repo.git("log", "--oneline", "D")
        assert "c2 grandchild commit" in repo.git("log", "--oneline", "C2")

        # Protocol step 4: B is now childless — remove it. Run from main (outside B's subtree).
        monkeypatch.chdir(repo.work)
        cmd_remove(_remove_ns("B"))

        after = discover()
        assert "B" not in after.parent_of  # unregistered from the tree
        assert not wt_b.exists()  # worktree torn down
        assert repo.git("rev-parse", "--verify", "B", check=False)  # branch ref kept
        assert after.parent_of["C"] == "main"  # children untouched by the removal
        assert after.parent_of["D"] == "main"

    def test_stale_child_replays_only_its_own_commit(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """A child that forked off an OLDER B commit still replays only its own commit onto
        the merge parent. The child's recorded fork is the exclude boundary; if the boundary
        were computed too low (below the child's fork), B's `b1` would be re-applied. `b1` and
        C touch different files, and main's copy of `b1`'s file is diverged after the squash,
        so a re-applied `b1` would collide and abort — proving it is not replayed."""
        repo.git("branch", "B", "main")
        repo.set_parent("B", "main")
        wt_b = repo.worktree("B", str(tmp_path / "wt-B"))
        _commit(repo, wt_b, "f_b1.txt", "b1\n", "b1 commit")

        # C forks off B at b1, so C's fork-commit is b1 (C never sees b2). c1 touches its own
        # file, so it always applies cleanly and cannot be the source of a collision.
        repo.git("branch", "C", "B")
        repo.set_parent("C", "B")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        _commit(repo, wt_c, "f_c1.txt", "c1\n", "c1 commit")

        # B advances after C forked; C is now stale relative to B's tip.
        _commit(repo, wt_b, "f_b2.txt", "b2\n", "b2 commit")

        # Squash-merge all of B (b1 + b2) into main, then diverge main's copy of b1's file so a
        # re-applied b1 would conflict.
        repo.checkout("main")
        repo.git("merge", "--squash", "B")
        repo.git("commit", "-m", "squash merge of B")
        repo.commit("f_b1.txt", "b1 edited on main\n", "main edits b1's file")

        monkeypatch.chdir(wt_c)
        cmd_rebase(_rebase_ns("main"))  # must not conflict/abort

        log = repo.git("log", "--oneline", "C")
        assert "c1 commit" in log
        assert "squash merge of B" in log
        assert "b1 commit" not in log
        assert "b2 commit" not in log
        assert discover().parent_of["C"] == "main"
        # b1 was not re-applied: main's diverged version survives on C, and c1's file is present.
        assert (wt_c / "f_b1.txt").read_text() == "b1 edited on main\n"
        assert (wt_c / "f_c1.txt").read_text() == "c1\n"

    def test_child_rebase_conflict_resumed_by_propagate(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """A conflict while hoisting a child onto the merge parent is resolved by re-running
        `git tree propagate <child>`, which finishes the rebase and cascades to the grandchild.
        B touches only its own file, so its empty replay does not add noise to the conflict."""
        repo.git("branch", "B", "main")
        repo.set_parent("B", "main")
        wt_b = repo.worktree("B", str(tmp_path / "wt-B"))
        _commit(repo, wt_b, "f_b.txt", "b\n", "b commit")

        repo.git("branch", "C", "B")
        repo.set_parent("C", "B")
        wt_c = repo.worktree("C", str(tmp_path / "wt-C"))
        _commit(repo, wt_c, "conflict.txt", "C version\n", "c commit")

        repo.git("branch", "C2", "C")
        repo.set_parent("C2", "C")
        wt_c2 = repo.worktree("C2", str(tmp_path / "wt-C2"))
        _commit(repo, wt_c2, "gc.txt", "gc\n", "grandchild commit")

        # Squash-merge B into main, then have main create conflict.txt with different content
        # so hoisting C conflicts.
        repo.checkout("main")
        repo.git("merge", "--squash", "B")
        repo.git("commit", "-m", "squash merge of B")
        repo.commit("conflict.txt", "main version\n", "main creates conflict.txt")

        monkeypatch.chdir(wt_c)
        with pytest.raises(SystemExit):
            cmd_rebase(_rebase_ns("main"))

        # Resolve and resume via the documented recovery path: re-run the propagate of the child.
        (wt_c / "conflict.txt").write_text("resolved\n")
        repo.git("add", "conflict.txt", cwd=wt_c)
        cmd_propagate(
            argparse.Namespace(branch="C", dry_run=False, no_auto_rerere=False, yes=False)
        )

        graph = discover()
        assert graph.parent_of["C"] == "main"
        assert graph.parent_of["C2"] == "C"
        assert "c commit" in repo.git("log", "--oneline", "C")
        assert (wt_c / "conflict.txt").read_text() == "resolved\n"
        # The cascade reached the grandchild: C2 sits on the rebased C, which now carries main.
        c2_log = repo.git("log", "--oneline", "C2")
        assert "grandchild commit" in c2_log
        assert "main creates conflict.txt" in c2_log
