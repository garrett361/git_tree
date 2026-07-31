from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from git_tree.cli import TreeError, _has_active_rebase, cmd_propagate, discover

from .conftest import RepoHelper, cli_args


def _ns(
    *,
    dry_run: bool = False,
    no_auto_rerere: bool = False,
    branch: str | None = None,
    yes: bool = False,
) -> object:
    return cli_args(dry_run=dry_run, no_auto_rerere=no_auto_rerere, branch=branch, yes=yes)


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
        assert "git tree propagate" in err

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

    def test_dry_run_previews_without_modifying(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "on b", cwd=wt_b)
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "first new on main")
        repo.commit("m3.txt", "m3", "second new on main")

        b_tip_before = repo.git("rev-parse", "b")
        # dry-run overrides --yes: preview only, confirm never consulted, no cascade.
        monkeypatch.setattr("git_tree.cli.confirm", _no_confirm)
        cmd_propagate(_ns(dry_run=True, yes=True))

        assert repo.git("rev-parse", "b") == b_tip_before
        out = capsys.readouterr().out
        assert "Propagating from" in out
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


class TestPropagateBranchArg:
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
        """An in-scope branch left mid-rebase with UNRESOLVED conflicts aborts propagation,
        pointing the user at the resume command (its onto is b's parent, so it's a would-be
        resume that just isn't resolved yet)."""
        repo.commit("shared.txt", "base", "base commit")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b modifies shared", cwd=wt_b)

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")
        # Trigger a conflicting rebase onto main (b's parent) — worktree becomes detached
        repo.git("rebase", "--onto", "main", "main~1", cwd=wt_b, check=False)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit) as exc:
            cmd_propagate(_ns())

        assert exc.value.code == 4
        err = capsys.readouterr().err
        assert "Resolve the conflicts" in err and "git tree propagate" in err
        assert "b" in err


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
        # A tree-child mid-rebase (unresolved) in the PRIMARY worktree (no linked worktree) must
        # be detected in the preflight, not misreported as "needs a worktree".
        repo.commit("shared.txt", "base", "base commit")
        repo.branch("b", parent="main")
        repo.checkout("b")
        repo.commit("shared.txt", "from b", "b modifies shared")

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "main modifies shared")

        # Start a conflicting rebase of b in the primary worktree, leave it UNRESOLVED →
        # HEAD detached, rebase still in progress with unmerged files.
        repo.checkout("b")
        repo.git("rebase", "main", check=False)

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit) as exc:
            cmd_propagate(_ns(branch="main"))

        assert exc.value.code == 4
        err = capsys.readouterr().err
        assert "Resolve the conflicts" in err and "b" in err
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


class TestTwoTreesResumeIndependently:
    def _stuck_tree(self, repo: RepoHelper, root: str, child: str, shared: str, monkeypatch):
        """Build root -> child (child in its own worktree), then conflict + propagate so the
        child's worktree is left mid-rebase. Returns the child's worktree path."""
        repo.git("branch", root, "main")
        repo.git("branch", child, root)
        repo.set_parent(child, root)
        wt = repo.worktree(child)
        (wt / shared).write_text(f"{child} version")
        repo.git("add", shared, cwd=wt)
        repo.git("commit", "-m", f"{child} edits {shared}", cwd=wt)

        repo.checkout(root)
        repo.commit(shared, f"{root} version", f"{root} edits {shared}")
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch=root))
        return wt

    def test_resuming_one_tree_leaves_the_other_stuck(self, repo: RepoHelper, monkeypatch) -> None:
        # Two independent trees each mid-rebase. `propagate <root>` names the tree to resume,
        # so no cwd disambiguation is needed and the other tree is untouched.
        wt_c1 = self._stuck_tree(repo, "r1", "c1", "s1.txt", monkeypatch)
        wt_c2 = self._stuck_tree(repo, "r2", "c2", "s2.txt", monkeypatch)
        assert _has_active_rebase(wt_c1) and _has_active_rebase(wt_c2)

        (wt_c1 / "s1.txt").write_text("resolved")
        repo.git("add", "s1.txt", cwd=wt_c1)
        cmd_propagate(_ns(branch="r1"))  # resume tree 1 by naming it; no prompt (a resume)

        assert not _has_active_rebase(wt_c1)  # c1 finished
        assert "r1 edits s1.txt" in repo.git("log", "--oneline", "c1")
        assert _has_active_rebase(wt_c2)  # the other tree is untouched

        (wt_c2 / "s2.txt").write_text("resolved")
        repo.git("add", "s2.txt", cwd=wt_c2)
        cmd_propagate(_ns(branch="r2"))
        assert not _has_active_rebase(wt_c2)


class TestStashPopConflict:
    def test_stash_pop_conflict_is_nonfatal_and_reported(
        self, repo: RepoHelper, monkeypatch, capsys, tmp_path
    ) -> None:
        """A dirty change that collides with the rebased tip: the rebase still moves the
        branch ref, and the failed stash pop is reported (not fatal), leaving the worktree
        for the user."""
        repo.commit("x.txt", "orig", "base with x")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "x.txt").write_text("b dirty")  # uncommitted, collides on pop

        repo.checkout("main")
        repo.commit("x.txt", "main new", "main changes x")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_propagate(_ns())  # must NOT raise: a pop conflict is non-fatal

        out = capsys.readouterr().out
        assert "stash pop conflict" in out
        # The rebase itself succeeded: b's ref moved onto main's new tip.
        assert repo.git("rev-parse", "b") == repo.git("rev-parse", "main")
        # The failed pop left the collision in the worktree for the user to resolve.
        assert "<<<<<<<" in (wt_b / "x.txt").read_text()


class TestPropagateResumeScope:
    """Resume is `git tree propagate <branch>` re-run: it finishes the interrupted rebase and
    cascades only `<branch>`'s subtree, so a sibling outside that subtree is never touched."""

    def _tree(self, repo: RepoHelper, *, sibling_conflicts: bool) -> tuple[Path, Path, Path]:
        """Root R with an out-of-scope sibling S that carries drift, an origin A with no
        drift, and A's child A1 that conflicts when A's own subtree is propagated. R is
        advanced on a file none of A/A1/S touch, so today's root-scoped resume rebases the
        drifted S cleanly (sibling_conflicts=False) or into a conflict (True). Returns the
        A, A1, S worktrees."""
        repo.commit("shared.txt", "original", "base shared")
        repo.git("branch", "R", "main")

        repo.git("branch", "S", "R")
        repo.set_parent("S", "R")
        wt_S = repo.worktree("S")
        s_file = "s-shared.txt" if sibling_conflicts else "s-only.txt"
        (wt_S / s_file).write_text("S version")
        repo.git("add", s_file, cwd=wt_S)
        repo.git("commit", "-m", "S edits its file", cwd=wt_S)

        # Advance R after S forked, so S drifts. On conflict variant R touches the same file
        # as S; otherwise a file disjoint from everything so A/A1 rebase onto R cleanly.
        repo.checkout("R")
        if sibling_conflicts:
            repo.commit("s-shared.txt", "R version", "R edits s-shared")
        else:
            repo.commit("r-advance.txt", "r", "R advances")

        repo.git("branch", "A", "R")  # forked at advanced R: no drift
        repo.set_parent("A", "R")
        wt_A = repo.worktree("A")
        (wt_A / "a-only.txt").write_text("a")
        repo.git("add", "a-only.txt", cwd=wt_A)
        repo.git("commit", "-m", "A adds a-only", cwd=wt_A)

        repo.git("branch", "A1", "A")  # forked before A's shared edit -> conflicts
        repo.set_parent("A1", "A")
        wt_A1 = repo.worktree("A1")
        (wt_A1 / "shared.txt").write_text("A1 version")
        repo.git("add", "shared.txt", cwd=wt_A1)
        repo.git("commit", "-m", "A1 edits shared", cwd=wt_A1)

        (wt_A / "shared.txt").write_text("A version")
        repo.git("add", "shared.txt", cwd=wt_A)
        repo.git("commit", "-m", "A edits shared", cwd=wt_A)
        return wt_A, wt_A1, wt_S

    def test_resume_finishes_descendant_and_leaves_sibling_untouched(
        self, repo: RepoHelper
    ) -> None:
        _wt_A, wt_A1, _wt_S = self._tree(repo, sibling_conflicts=False)
        s_before = repo.git("rev-parse", "S")
        s_fork_before = repo.git("config", "branch.S.tree-fork-commit")

        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="A", yes=True))
        assert _has_active_rebase(wt_A1)

        (wt_A1 / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_A1)
        cmd_propagate(_ns(branch="A"))  # resume: re-run the same command, no prompt

        assert not _has_active_rebase(wt_A1)
        # A's own subtree was propagated: A1 sits on top of A's shared edit.
        assert "A edits shared" in repo.git("log", "--oneline", "A1")
        # The out-of-scope sibling S never moved and its fork boundary is untouched.
        assert repo.git("rev-parse", "S") == s_before
        assert repo.git("config", "branch.S.tree-fork-commit") == s_fork_before

    def test_resume_still_unresolved_refuses(self, repo: RepoHelper) -> None:
        _wt_A, wt_A1, _wt_S = self._tree(repo, sibling_conflicts=False)
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="A", yes=True))
        # Re-run without resolving: the preflight refuses and names the resume command.
        with pytest.raises(TreeError) as exc:
            cmd_propagate(_ns(branch="A"))
        assert exc.value.code == 4
        assert exc.value.kind == "unresolved_conflicts"
        assert "git tree propagate A" in exc.value.message
        assert _has_active_rebase(wt_A1)  # nothing resumed

    def test_resume_message_names_propagate(self, repo: RepoHelper, capsys) -> None:
        self._tree(repo, sibling_conflicts=False)
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="A", yes=True))
        err = capsys.readouterr().err
        assert "git tree propagate A" in err
        assert "no need to run `git rebase --continue`" in err

    def test_manual_finish_then_resume_is_tolerated(self, repo: RepoHelper) -> None:
        _wt_A, wt_A1, _wt_S = self._tree(repo, sibling_conflicts=False)
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="A", yes=True))
        # Finish the rebase by hand, then re-run propagate — must reach the same end state.
        (wt_A1 / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_A1)
        repo.git("-c", "core.editor=true", "rebase", "--continue", cwd=wt_A1)
        assert not _has_active_rebase(wt_A1)

        # Nothing is mid-rebase now, so this is a fresh propagate (it prompts): idempotent
        # no-op re-rebase of A1 that corrects the fork.
        cmd_propagate(_ns(branch="A", yes=True))

        assert not _has_active_rebase(wt_A1)
        assert "A edits shared" in repo.git("log", "--oneline", "A1")
        assert repo.git("config", "branch.A1.tree-fork-commit") == repo.git("rev-parse", "A")

    def test_resume_with_unrelated_unstaged_change_keeps_the_commit(self, repo: RepoHelper) -> None:
        """An unstaged tracked edit must not cost the branch its commit.

        `git rebase --continue` refuses while any tracked file is modified but unstaged, even
        though the conflict itself is resolved and staged. The index then has no unmerged
        entries, so a `--skip` here would hard-reset the commit being replayed, the resolution,
        and the unstaged edit, and report success.
        """
        _wt_A, wt_A1, _wt_S = self._tree(repo, sibling_conflicts=False)
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="A", yes=True))

        (wt_A1 / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_A1)
        # An unrelated tracked file, edited but not staged: the whole trigger.
        (wt_A1 / "a-only.txt").write_text("work in progress")

        with pytest.raises(SystemExit) as exc:
            cmd_propagate(_ns(branch="A"))

        assert exc.value.code == 4
        assert "A1 edits shared" in repo.git("log", "--oneline", "A1")
        assert (wt_A1 / "a-only.txt").read_text() == "work in progress"
        assert _has_active_rebase(wt_A1)  # still resumable once the edit is dealt with

        # Stashing only the unrelated path is safe, and leaves the replay intact.
        repo.git("stash", "push", "--", "a-only.txt", cwd=wt_A1)
        cmd_propagate(_ns(branch="A"))

        assert not _has_active_rebase(wt_A1)
        assert "A1 edits shared" in repo.git("log", "--oneline", "A1")
        assert (wt_A1 / "shared.txt").read_text() == "resolved"

    @pytest.mark.xfail(
        strict=True,
        reason="the advised stash pathspec is built from `git diff --name-only HEAD`, which "
        "includes the staged resolution, so running it empties the replay",
    )
    def test_following_the_printed_advice_keeps_the_commit(self, repo: RepoHelper) -> None:
        """The command the refusal prints has to be safe to run verbatim.

        The test above stashes only the unrelated path, which is not what the tool tells you to
        do. `propagate` is documented as the single runnable resume and `--json` ships the same
        advice as a `remedy` argv, so an agent runs it with no human in the loop.
        """
        _wt_A, wt_A1, _wt_S = self._tree(repo, sibling_conflicts=False)
        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="A", yes=True))

        (wt_A1 / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_A1)
        (wt_A1 / "a-only.txt").write_text("work in progress")

        with pytest.raises(SystemExit) as exc:
            cmd_propagate(_ns(branch="A"))

        # Take the command out of the message rather than hardcoding it, so this tracks whatever
        # the tool actually advises.
        advice = next((ln for ln in exc.value.message.splitlines() if "stash push" in ln), None)
        assert advice, f"the refusal named no stash command:\n{exc.value.message}"
        subprocess.run(
            shlex.split(advice[advice.index("git ") :]),
            cwd=wt_A1,
            check=True,
            capture_output=True,
        )
        cmd_propagate(_ns(branch="A"))

        assert "A1 edits shared" in repo.git("log", "--oneline", "A1")

    def test_resume_covers_named_branch_whole_subtree(self, repo: RepoHelper) -> None:
        # Resuming `propagate R` finishes the stuck child AND propagates the other drifted child.
        repo.commit("shared.txt", "original", "base shared")
        repo.git("branch", "R", "main")
        repo.git("branch", "C1", "R")
        repo.set_parent("C1", "R")
        wt_C1 = repo.worktree("C1")
        (wt_C1 / "shared.txt").write_text("C1 version")
        repo.git("add", "shared.txt", cwd=wt_C1)
        repo.git("commit", "-m", "C1 edits shared", cwd=wt_C1)
        repo.git("branch", "C2", "R")
        repo.set_parent("C2", "R")
        wt_C2 = repo.worktree("C2")
        (wt_C2 / "c2-only.txt").write_text("c2")
        repo.git("add", "c2-only.txt", cwd=wt_C2)
        repo.git("commit", "-m", "C2 adds c2-only", cwd=wt_C2)
        repo.checkout("R")
        repo.commit("shared.txt", "R version", "R advances shared")  # C1, C2 both drift
        c2_before = repo.git("rev-parse", "C2")

        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="R", yes=True))
        assert _has_active_rebase(wt_C1)
        (wt_C1 / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_C1)
        cmd_propagate(_ns(branch="R"))

        assert not _has_active_rebase(wt_C1)
        assert repo.git("rev-parse", "C2") != c2_before  # the other child was propagated
        assert "R advances shared" in repo.git("log", "--oneline", "C2")


class TestPropagateResumeGuards:
    def test_foreign_rebase_is_refused(self, repo: RepoHelper) -> None:
        # A descendant left mid-rebase onto something that is NOT its tree-parent (a hand-started
        # rebase) must be refused, not silently finished onto the wrong base.
        repo.commit("shared.txt", "original", "base")
        repo.git("branch", "A", "main")
        repo.set_parent("A", "main")
        wt_A = repo.worktree("A")
        (wt_A / "a.txt").write_text("a")
        repo.git("add", "a.txt", cwd=wt_A)
        repo.git("commit", "-m", "A adds a", cwd=wt_A)
        repo.git("branch", "A1", "A")
        repo.set_parent("A1", "A")
        wt_A1 = repo.worktree("A1")
        (wt_A1 / "shared.txt").write_text("A1 version")
        repo.git("add", "shared.txt", cwd=wt_A1)
        repo.git("commit", "-m", "A1 edits shared", cwd=wt_A1)
        # X diverges from main (not an ancestor of A), and conflicts with A1 on shared.txt.
        repo.checkout("main")
        repo.commit("shared.txt", "X version", "X edits shared")
        repo.git("branch", "X", "main")

        # Hand-start a rebase of A1 onto X — leaves A1 mid-rebase onto a foreign base.
        repo.git("-c", "core.editor=true", "rebase", "X", cwd=wt_A1, check=False)
        assert _has_active_rebase(wt_A1)

        with pytest.raises(TreeError) as exc:
            cmd_propagate(_ns(branch="A", yes=True))
        assert exc.value.code == 4
        assert "not started by git-tree" in exc.value.message
        assert _has_active_rebase(wt_A1)  # left as-is

    def test_mid_merge_is_refused(self, repo: RepoHelper) -> None:
        """A merge stopped with `--no-commit` must not be silently thrown away.

        Once resolutions are staged, status shows `M ` rather than `UU`, so the conflicted-files
        check never fires, and `_rebase_branch`'s unconditional `git stash push` clears
        `MERGE_HEAD` along with the rest of the sequencer state. The merge cannot be continued
        afterwards.
        """
        repo.commit("shared.txt", "base", "base")
        repo.git("branch", "A", "main")
        repo.set_parent("A", "main")
        wt_A = repo.worktree("A")
        (wt_A / "a.txt").write_text("a")
        repo.git("add", "a.txt", cwd=wt_A)
        repo.git("commit", "-m", "A adds a", cwd=wt_A)
        repo.git("branch", "A1", "A")
        repo.set_parent("A1", "A")
        wt_A1 = repo.worktree("A1")
        (wt_A1 / "a1.txt").write_text("a1")
        repo.git("add", "a1.txt", cwd=wt_A1)
        repo.git("commit", "-m", "A1 adds a1", cwd=wt_A1)

        # A side branch A1 is mid-merging. Disjoint files, so it stages cleanly: no `UU`.
        repo.git("branch", "side", "A")
        wt_side = repo.worktree("side")
        (wt_side / "side.txt").write_text("side")
        repo.git("add", "side.txt", cwd=wt_side)
        repo.git("commit", "-m", "side adds side", cwd=wt_side)
        repo.git("merge", "--no-commit", "--no-ff", "side", cwd=wt_A1, check=False)
        merge_head = repo.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=wt_A1, check=False)
        assert merge_head, "expected a merge in progress"

        with pytest.raises(TreeError) as exc:
            cmd_propagate(_ns(branch="A", yes=True))

        assert exc.value.code == 4
        # The claim is that the merge survives, and only this asserts it. Staged resolutions show
        # as `M `, and a stashed `side.txt` comes back on the pop, so the two checks below hold
        # just as well on the path where the stash silently cleared MERGE_HEAD.
        assert (
            repo.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=wt_A1, check=False)
            == merge_head
        )
        assert "side.txt" in repo.git("status", "--porcelain", cwd=wt_A1)

    def test_hand_run_interactive_rebase_is_refused(self, repo: RepoHelper) -> None:
        """A user's own `git rebase -i <parent>` must not be adopted and driven to completion.

        Its `onto` equals the tree-parent, so the ownership test alone cannot tell it apart from
        git-tree's own cascade. Driving it finishes someone else's rebase past every stop point
        and then reports `resumed`, even though the branch never received the parent's commit.
        """
        repo.commit("shared.txt", "base", "base")
        repo.git("branch", "A", "main")
        repo.set_parent("A", "main")
        wt_A = repo.worktree("A")
        (wt_A / "a.txt").write_text("one")
        repo.git("add", "a.txt", cwd=wt_A)
        repo.git("commit", "-m", "A adds a", cwd=wt_A)
        repo.git("branch", "A1", "A")
        repo.set_parent("A1", "A")
        wt_A1 = repo.worktree("A1")
        (wt_A1 / "a1.txt").write_text("a1")
        repo.git("add", "a1.txt", cwd=wt_A1)
        repo.git("commit", "-m", "A1 adds a1", cwd=wt_A1)

        # Stopped at an `edit` with a clean worktree: the user has not started rewriting yet, so
        # nothing is dirty and the empty-replay guard does not apply. Driving on from here
        # finishes their rebase for them.
        repo.rebase_interactive(wt_A1, "A", "edit")
        assert _has_active_rebase(wt_A1)

        with pytest.raises(TreeError) as exc:
            cmd_propagate(_ns(branch="A", yes=True))

        assert exc.value.code == 4
        # Name the gate: a blanket "no rebase is ever git-tree's" regression, which would break
        # every legitimate resume in the tool, would also satisfy a bare code check.
        assert "not started by git-tree" in exc.value.message
        assert _has_active_rebase(wt_A1)  # still theirs to finish or abort

    def test_named_branch_mid_am_is_refused(self, repo: RepoHelper, tmp_path) -> None:
        """A `git am` in the NAMED branch's worktree must not be driven as if it were a resume.

        `git am` uses `rebase-apply/` with no `onto` file, so ownership is unknowable.
        `_require_clean_state` already treats that as foreign, but it only ever sees descendants;
        the named branch reaches `_advance_branch` directly, which drove anything whose `onto`
        could not be read.
        """
        repo.commit("shared.txt", "original", "base")
        repo.git("branch", "A", "main")
        repo.set_parent("A", "main")
        wt_A = repo.worktree("A")
        repo.git("branch", "A1", "A")
        repo.set_parent("A1", "A")
        repo.worktree("A1")

        # A patch that cannot apply to A: both sides touch shared.txt differently.
        repo.checkout("main")
        repo.commit("shared.txt", "patch version", "patch edits shared")
        repo.git("format-patch", "-1", "main", "-o", str(tmp_path / "patches"))
        repo.git("reset", "--hard", "HEAD~1")
        (wt_A / "shared.txt").write_text("A version")
        repo.git("add", "shared.txt", cwd=wt_A)
        repo.git("commit", "-m", "A edits shared", cwd=wt_A)

        patch = next((tmp_path / "patches").glob("*.patch"))
        repo.git("am", str(patch), cwd=wt_A, check=False)
        assert _has_active_rebase(wt_A)  # rebase-apply/ is in progress

        with pytest.raises(TreeError) as exc:
            cmd_propagate(_ns(branch="A", yes=True))
        assert exc.value.code == 4
        # `_advance_branch`'s own wording, not `_require_clean_state`'s: the named branch must be
        # refused by the gate that handles it directly.
        assert "git-tree did not start it" in exc.value.message
        assert _has_active_rebase(wt_A)  # the am is left for the user to finish or abort

    @pytest.mark.xfail(
        strict=True,
        reason="the resume returns at the base the rebase began at instead of falling through to "
        "the ordinary propagate step, so the child keeps drift the run should have removed",
    )
    def test_resume_leaves_the_child_on_the_live_parent(self, repo: RepoHelper) -> None:
        """Committing to the parent while resolving a conflict is ordinary, and drift by itself is
        legal. What is not legal is a `propagate` that exits ok having left drift it could remove.

        The base the interrupted rebase replayed onto is the right thing to record when
        `--continue` finishes: those commits really did land there, and naming the parent's newer
        tip while the child sits below it would set an exclude boundary that swallows the child's
        own commits on the next run. That value is an intermediate, though, not a terminal one.
        Finishing the resume has to be followed by the branch's ordinary propagate step, which
        lands it on the live parent and moves the fork with it.
        """
        repo.commit("shared.txt", "original", "base")
        repo.git("branch", "A", "main")
        repo.set_parent("A", "main")
        wt_A = repo.worktree("A")
        (wt_A / "shared.txt").write_text("A version")
        repo.git("add", "shared.txt", cwd=wt_A)
        repo.git("commit", "-m", "A edits shared", cwd=wt_A)
        repo.git("branch", "A1", "A")
        repo.set_parent("A1", "A")
        wt_A1 = repo.worktree("A1")
        (wt_A1 / "shared.txt").write_text("A1 version")
        repo.git("add", "shared.txt", cwd=wt_A1)
        repo.git("commit", "-m", "A1 edits shared", cwd=wt_A1)
        (wt_A / "shared.txt").write_text("A version 2")
        repo.git("add", "shared.txt", cwd=wt_A)
        repo.git("commit", "-m", "A edits shared again", cwd=wt_A)

        with pytest.raises(SystemExit):
            cmd_propagate(_ns(branch="A", yes=True))
        assert _has_active_rebase(wt_A1)

        # The user fixes something on the parent while they are in there resolving.
        (wt_A / "later.txt").write_text("later")
        repo.git("add", "later.txt", cwd=wt_A)
        repo.git("commit", "-m", "A gains a fix during the conflict", cwd=wt_A)

        (wt_A1 / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_A1)
        cmd_propagate(_ns(branch="A"))  # returns ok today

        assert not _has_active_rebase(wt_A1)
        # Both halves of "A1 is a child of A": the shape, and the boundary the next propagate
        # replays from. A correct shape over a stale fork still misleads the following run.
        assert "A gains a fix during the conflict" in repo.git("log", "--oneline", "A1")
        assert repo.git("config", "branch.A1.tree-fork-commit") == repo.git("rev-parse", "A")
        # Landing on the live parent must not cost A1 its own work: an exclude boundary set to
        # the parent tip while A1 still sits below it would replay nothing.
        assert "A1 edits shared" in repo.git("log", "--oneline", "A1")

    @pytest.mark.xfail(
        strict=True,
        reason="the refusal always says `not onto <parent>`, which is false when the rebase was "
        "refused for being the user's own interactive session",
    )
    def test_interactive_refusal_does_not_misreport_the_base(self, repo: RepoHelper) -> None:
        """A wrong reason sends the user to check something that is already correct.

        `_advance_branch` has one message for two rejections. Here `onto` *is* the tree-parent and
        the rebase was refused for being interactive, so the text names the wrong problem.
        """
        repo.commit("shared.txt", "base", "base")
        repo.git("branch", "A", "main")
        repo.set_parent("A", "main")
        wt_A = repo.worktree("A")
        (wt_A / "a.txt").write_text("a")
        repo.git("add", "a.txt", cwd=wt_A)
        repo.git("commit", "-m", "A adds a", cwd=wt_A)

        repo.rebase_interactive(wt_A, "main", "edit")  # onto IS A's tree-parent
        assert _has_active_rebase(wt_A)

        with pytest.raises(TreeError) as exc:
            cmd_propagate(_ns(branch="A", yes=True))

        assert exc.value.code == 4
        assert "not onto main" not in exc.value.message
