from __future__ import annotations

import argparse
import shutil

import pytest

from git_tree.cli import cmd_propagate, discover

from .conftest import RepoHelper


def _ns(
    *,
    dry_run: bool = False,
    no_auto_rerere: bool = False,
    branch: str | None = None,
    yes: bool = False,
) -> object:
    return argparse.Namespace(
        dry_run=dry_run, no_auto_rerere=no_auto_rerere, branch=branch, yes=yes
    )


def _no_confirm(_message: str) -> bool:
    raise AssertionError("confirm should not be consulted")


class TestPropagate:
    def test_yes_skips_confirmation(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.commit("a1.txt", "a1", "commit on main for b")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "commit on b", cwd=wt_b)
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "new commit on main")

        monkeypatch.setattr("git_tree.cli.confirm", _no_confirm)
        cmd_propagate(_ns(yes=True))

        assert "new commit on main" in repo.git("log", "--oneline", "b")

    def test_dry_overrides_yes(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.commit("a1.txt", "a1", "commit on main for b")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "commit on b", cwd=wt_b)
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "new commit on main")

        monkeypatch.setattr("git_tree.cli.confirm", _no_confirm)
        cmd_propagate(_ns(dry_run=True, yes=True))  # preview only, no cascade

        assert "new commit on main" not in repo.git("log", "--oneline", "b")

    def test_linear_cascade(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.commit("a1.txt", "a1", "commit on main for b")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "commit on b", cwd=wt_b)
        repo.branch("c", parent="b")
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        (wt_c / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt_c)
        repo.git("commit", "-m", "commit on c", cwd=wt_c)

        repo.checkout("main")
        repo.commit("a2.txt", "a2", "new commit on main")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())

        b_contains = repo.git("log", "--oneline", "b")
        assert "new commit on main" in b_contains

        c_contains = repo.git("log", "--oneline", "c")
        assert "new commit on main" in c_contains
        assert "commit on b" in c_contains

    def test_no_descendants_is_noop(self, repo: RepoHelper, monkeypatch, capsys, tmp_path) -> None:
        repo.branch("leaf", parent="main")
        repo.worktree("leaf", str(tmp_path / "wt-leaf"))
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="leaf"))
        out = capsys.readouterr().out
        assert "No descendants" in out

    def test_conflict_stops_and_exits(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        repo.commit("shared.txt", "original", "base")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies shared", cwd=wt_b)

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared (conflict)")

        monkeypatch.setattr("builtins.input", lambda _: "y")

        with pytest.raises(SystemExit):
            cmd_propagate(_ns())

        err = capsys.readouterr().err
        assert "CONFLICT" in err
        # The message must point the user at the single-command resume, not leave them
        # stranded to run raw git plus a follow-up propagate.
        assert "git tree continue" in err

    def test_already_up_to_date(self, repo: RepoHelper, monkeypatch, capsys, tmp_path) -> None:
        repo.branch("b", parent="main")
        repo.worktree("b", str(tmp_path / "wt-b"))
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())
        out = capsys.readouterr().out
        assert "b" in out

    def test_child_no_unique_commits(self, repo: RepoHelper, monkeypatch, capsys, tmp_path) -> None:
        """Branch with no unique commits beyond parent still propagates cleanly."""
        repo.branch("b", parent="main")
        repo.worktree("b", str(tmp_path / "wt-b"))
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main past b fork")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())

        b_log = repo.git("log", "--oneline", "b")
        assert "advance main past b fork" in b_log

    def test_grandchild_no_unique_commits_cascades(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        repo.branch("b", parent="main")
        repo.worktree("b", str(tmp_path / "wt-b"))
        repo.branch("c", parent="b")
        repo.worktree("c", str(tmp_path / "wt-c"))
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())

        b_log = repo.git("log", "--oneline", "b")
        assert "advance main" in b_log
        c_log = repo.git("log", "--oneline", "c")
        assert "advance main" in c_log

    def test_confirmation_decline_is_noop(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "on b", cwd=wt_b)
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")

        b_tip_before = repo.git("rev-parse", "b")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        cmd_propagate(_ns())

        assert repo.git("rev-parse", "b") == b_tip_before

    def test_dirty_worktree_stash_roundtrip(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.commit("a1.txt", "a1", "base for b")
        repo.branch("b", parent="main")
        wt_path = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_path / "uncommitted.txt").write_text("dirty content")

        repo.checkout("main")
        repo.commit("a2.txt", "a2", "new on main")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())

        assert (wt_path / "uncommitted.txt").exists()
        assert (wt_path / "uncommitted.txt").read_text() == "dirty content"

    def test_dry_does_not_modify(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.commit("a1.txt", "a1", "base for b")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "on b", cwd=wt_b)
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "advance main")

        b_tip_before = repo.git("rev-parse", "b")
        cmd_propagate(_ns(dry_run=True))

        assert repo.git("rev-parse", "b") == b_tip_before
        out = capsys.readouterr().out
        assert "Propagating from" in out

    def test_preview_shows_pending_commit_counts(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "on b", cwd=wt_b)
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "first new on main")
        repo.commit("m3.txt", "m3", "second new on main")

        cmd_propagate(_ns(dry_run=True))

        out = capsys.readouterr().out
        assert "[2 new]" in out

    def test_preview_no_count_when_up_to_date(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "on b", cwd=wt_b)

        repo.checkout("main")
        cmd_propagate(_ns(dry_run=True))

        out = capsys.readouterr().out
        assert "[" not in out

    def test_equivalent_cherry_picked_patches_skipped(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """Patches cherry-picked to child (same content, different SHA) don't conflict."""
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))

        # Add a commit to main
        repo.checkout("main")
        repo.commit("f1.txt", "feature", "add feature")
        main_tip = repo.git("rev-parse", "main")

        # Cherry-pick it into b (same content, different SHA)
        repo.git("cherry-pick", main_tip, cwd=wt_b)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())

        b_log = repo.git("log", "--oneline", "b")
        assert "add feature" in b_log


class TestPropagateRerere:
    def test_auto_rerere_continues_through_known_conflict(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        repo.enable_rerere()
        repo.commit("shared.txt", "original", "base")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies shared", cwd=wt_b)
        b_original = repo.git("rev-parse", "b")

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")

        # Record rerere resolution in b's worktree
        repo.git("rebase", "--onto", "main", b_original + "~1", cwd=wt_b, check=False)
        (wt_b / "shared.txt").write_text("resolved content")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("rebase", "--continue", cwd=wt_b)

        # Reset b back to original state
        repo.git("reset", "--hard", b_original, cwd=wt_b)

        # Now propagate — rerere should auto-resolve
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())

        out = capsys.readouterr().out
        assert "rerere" in out
        # The side-effecting rerere staging is echoed, not run silently (transparency goal).
        assert "+ git add -u" in out
        b_log = repo.git("log", "--oneline", "b")
        assert "main modifies shared" in b_log

    def test_auto_rerere_stops_on_unknown_conflict(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        repo.enable_rerere()
        repo.commit("shared.txt", "original", "base")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies shared", cwd=wt_b)
        b_original = repo.git("rev-parse", "b")

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")

        # No rerere recording — propagate should still stop
        monkeypatch.setattr("builtins.input", lambda _: "y")

        with pytest.raises(SystemExit):
            cmd_propagate(_ns())

        assert repo.git("rev-parse", "b") == b_original

    def test_auto_rerere_disabled_with_flag(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.enable_rerere()
        repo.commit("shared.txt", "original", "base")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies shared", cwd=wt_b)
        b_original = repo.git("rev-parse", "b")

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")

        # Even though rerere is enabled, --no-auto-rerere skips the loop
        monkeypatch.setattr("builtins.input", lambda _: "y")

        with pytest.raises(SystemExit):
            cmd_propagate(_ns(no_auto_rerere=True))

        assert repo.git("rev-parse", "b") == b_original

    def test_auto_rerere_multi_commit_branch(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        repo.enable_rerere()
        repo.commit("f1.txt", "original1", "base1")
        repo.commit("f2.txt", "original2", "base2")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "f1.txt").write_text("b-version1")
        repo.git("add", "f1.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies f1", cwd=wt_b)
        (wt_b / "f2.txt").write_text("b-version2")
        repo.git("add", "f2.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies f2", cwd=wt_b)
        b_original = repo.git("rev-parse", "b")

        repo.checkout("main")
        repo.commit("f1.txt", "main-version1", "main modifies f1")
        repo.commit("f2.txt", "main-version2", "main modifies f2")

        # Record resolutions in b's worktree
        repo.git("rebase", "--onto", "main", b_original + "~2", cwd=wt_b, check=False)
        (wt_b / "f1.txt").write_text("resolved1")
        repo.git("add", "f1.txt", cwd=wt_b)
        repo.git("rebase", "--continue", cwd=wt_b, check=False)
        (wt_b / "f2.txt").write_text("resolved2")
        repo.git("add", "f2.txt", cwd=wt_b)
        repo.git("rebase", "--continue", cwd=wt_b)

        # Reset and propagate
        repo.git("reset", "--hard", b_original, cwd=wt_b)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())

        out = capsys.readouterr().out
        assert "rerere" in out
        assert "resolved" in repo.git("show", "b:f1.txt")


class TestPropagateBranchArg:
    def test_propagate_from_named_branch(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        repo.commit("a1.txt", "a1", "advance main")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "on b", cwd=wt_b)
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "new on main")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="main"))

        b_log = repo.git("log", "--oneline", "b")
        assert "new on main" in b_log

    def test_propagate_from_non_current_branch(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        repo.commit("a1.txt", "a1", "advance main")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "on b", cwd=wt_b)
        repo.branch("c", parent="b")
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        (wt_c / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt_c)
        repo.git("commit", "-m", "on c", cwd=wt_c)

        repo.checkout("main")
        repo.commit("a2.txt", "a2", "new on main")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="main"))

        b_log = repo.git("log", "--oneline", "b")
        assert "new on main" in b_log
        c_log = repo.git("log", "--oneline", "c")
        assert "new on main" in c_log


class TestWorktreeValidation:
    def test_propagate_fails_without_worktree(self, repo: RepoHelper, monkeypatch, capsys) -> None:
        repo.branch("b", parent="main")
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns())

        err = capsys.readouterr().err
        assert "worktree" in err.lower()
        assert "b" in err

    def test_error_lists_all_missing(self, repo: RepoHelper, monkeypatch, capsys) -> None:
        repo.branch("b", parent="main")
        repo.branch("c", parent="main")
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns())

        err = capsys.readouterr().err
        assert "b" in err
        assert "c" in err

    def test_propagate_fails_with_active_rebase(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        """A branch with an active rebase (detached worktree) aborts propagation early."""
        repo.commit("shared.txt", "base", "base commit")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies shared", cwd=wt_b)

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")
        # Trigger a conflicting rebase — worktree becomes detached
        repo.git("rebase", "--onto", "main", "main~1", cwd=wt_b, check=False)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns())

        err = capsys.readouterr().err
        assert "not in a clean state" in err
        assert "b" in err

    def test_propagate_fails_with_rebase_in_progress(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        """Branch with resolved conflicts but pending --continue aborts propagation."""
        repo.commit("shared.txt", "base", "base commit")
        repo.branch("b", parent="main")
        repo.branch("c", parent="b")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        (wt_c / "shared.txt").write_text("from c")
        repo.git("add", "shared.txt", cwd=wt_c)
        repo.git("commit", "-m", "c modifies shared", cwd=wt_c)

        # Advance b to create a conflict for c
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies shared", cwd=wt_b)

        # Rebase c onto b, causing conflict
        repo.git("rebase", "--onto", "b", "b~1", cwd=wt_c, check=False)
        # Resolve the conflict but don't --continue
        (wt_c / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_c)

        # Propagate from main — c has rebase in progress
        # b's worktree is fine but c's is in active rebase (detached)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns())

        err = capsys.readouterr().err
        assert "c" in err


class TestPropagateMainWorktree:
    def test_propagate_to_branch_in_main_worktree(self, repo: RepoHelper, monkeypatch) -> None:
        """A tree-child checked out in the main worktree (no linked worktree) can be propagated."""
        repo.commit("a1.txt", "a1", "base")
        repo.branch("b", parent="main")
        repo.checkout("b")
        repo.commit("b1.txt", "b1", "on b")

        repo.checkout("main")
        repo.commit("a2.txt", "a2", "advance main")

        repo.checkout("b")
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns(branch="main"))

        b_log = repo.git("log", "--oneline", "b")
        assert "advance main" in b_log
        assert "on b" in b_log

    def test_rebase_in_primary_worktree_reported_as_unclean(
        self, repo: RepoHelper, monkeypatch, capsys
    ) -> None:
        # A tree-child mid-rebase in the PRIMARY worktree (no linked worktree) must be
        # detected as unclean, not misreported as "needs a worktree".
        repo.commit("shared.txt", "base", "base commit")
        repo.branch("b", parent="main")
        repo.checkout("b")
        repo.commit("shared.txt", "from b", "b modifies shared")

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")

        # Start a conflicting rebase of b in the primary worktree, resolve but don't
        # continue → HEAD detached, rebase still in progress.
        repo.checkout("b")
        repo.git("rebase", "main", check=False)
        (repo.work / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="main"))

        err = capsys.readouterr().err
        assert "not in a clean state" in err
        assert "rebase in progress" in err
        assert "need worktrees" not in err  # the pre-fix misreport


class TestPropagatePrunableWorktree:
    def test_deleted_worktree_dir_degrades_cleanly(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        # A worktree dir removed with `rm -rf` (not `git worktree prune`) is still listed
        # by git as prunable. discovery must skip it and degrade to "needs a worktree"
        # rather than crash with an uncaught FileNotFoundError.
        repo.commit("a1.txt", "a1", "advance main")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "on b", cwd=wt_b)
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "new on main")

        shutil.rmtree(wt_b)  # dir gone; git still reports the worktree as prunable

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):  # a FileNotFoundError would not be a SystemExit
            cmd_propagate(_ns(branch="main"))
        assert "need worktrees" in capsys.readouterr().err

    def test_detached_prunable_worktree_does_not_crash(self, repo: RepoHelper, tmp_path) -> None:
        # git's canonical prunable example is a DETACHED worktree; its head-name recovery
        # path (_git_dir) would FileNotFoundError on the deleted dir if not skipped.
        repo.branch("b", parent="main")
        det = tmp_path / "det"
        repo.git("worktree", "add", "--detach", str(det))
        shutil.rmtree(det)

        graph = discover()  # must not raise FileNotFoundError
        assert graph.parent_of["b"] == "main"
