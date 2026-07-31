from __future__ import annotations

import json
import os

import pytest

from git_tree.cli import TreeError, _has_active_rebase, cmd_remove, discover, main

from .conftest import RepoHelper, cli_args, stopped_rebase


def _ns(branch: str | None = None, yes: bool = False) -> object:
    return cli_args(branch=branch, yes=yes)


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


class TestRemoveMidRebase:
    """A worktree mid-rebase holds the only reference to work already replayed.

    The branch ref does not move until a rebase completes, so committed branch work is safe;
    what dies with the worktree is every conflict already resolved for earlier commits, which
    lives on the detached HEAD and in `.git/worktrees/<id>/logs/HEAD`, plus the tree config the
    documented resume needs. The dirty gate caught this only by accident, because a stopped
    rebase is usually dirty.
    """

    def test_refuses_and_leaves_the_rebase_intact(self, repo: RepoHelper, tmp_path) -> None:
        wt = stopped_rebase(repo, tmp_path)
        with pytest.raises(TreeError) as exc:
            cmd_remove(_ns(branch="A", yes=True))

        assert exc.value.code == 4
        # The word "rebase" alone also appears in the dirt gate's `git rebase --abort` advice,
        # so name the gate that must have fired.
        assert "a rebase is in progress" in exc.value.message
        assert wt.exists()
        assert _has_active_rebase(wt)  # not just present: still resumable
        assert "A" in discover().parent_of  # tree config intact, so a resume is still possible

    def test_force_still_removes(self, repo: RepoHelper, tmp_path) -> None:
        wt = stopped_rebase(repo, tmp_path)
        cmd_remove(cli_args(branch="A", yes=True, force=True))
        assert not wt.exists()

    def test_force_warns_naming_the_mid_rebase_branch(
        self, repo: RepoHelper, tmp_path, capsys
    ) -> None:
        wt = stopped_rebase(repo, tmp_path)
        cmd_remove(cli_args(branch="A", yes=True, force=True, dry_run=True))

        out = capsys.readouterr().out
        assert "will discard the rebase in progress" in out
        assert "A" in out and str(wt) in out
        assert _has_active_rebase(wt)  # --dry-run destroyed nothing


class TestRemoveDisclosesIgnoredFiles:
    def test_notice_is_printed_without_force(self, repo: RepoHelper, tmp_path, capsys) -> None:
        """Said on every path, not only under --force.

        `git status` never reports ignored files, so a clean worktree passes the dirt gate with a
        `.env` or a virtualenv still in it, and removal deletes them unannounced.
        """
        repo.branch("A", parent="main")
        repo.worktree("A", str(tmp_path / "wt-A"))

        cmd_remove(cli_args(branch="A", yes=True, dry_run=True))

        assert "git-ignored files included" in capsys.readouterr().out


class TestRemoveLockedWorktree:
    @pytest.mark.xfail(
        strict=True,
        reason="git escalates in two steps, unclean needs --force and locked needs --force twice, "
        "but _force_remove_worktree passes it once, so on a locked worktree stage 1 fails and the "
        "unconditional shutil.rmtree deletes it anyway. Fix: gate on the lock in cmd_remove "
        "alongside dirt and mid-rebase, and have stage 1 pass --force twice under --force so git "
        "does the removal instead of falling through to rmtree.",
    )
    def test_locked_worktree_is_not_removed(self, repo: RepoHelper, tmp_path) -> None:
        """`git worktree lock` is git's do-not-remove marker and must gate removal.

        git escalates in two steps: an unclean worktree needs `--force`, a locked one needs
        `--force` twice. git-tree passes it once, so on a locked worktree stage 1 fails and
        stage 2 `shutil.rmtree`s the directory anyway. The lock does not merely fail to protect,
        it diverts removal off git's bookkeeping-aware path onto the blind one, and its documented
        use (a worktree on a share that is not always mounted) is the worst case for that.
        """
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))
        repo.git("worktree", "lock", "--reason", "on a network share", str(wt))

        with pytest.raises(TreeError) as exc:
            cmd_remove(_ns(branch="A", yes=True))

        assert exc.value.code == 4
        assert wt.exists()
        assert "wt-A" in repo.git("worktree", "list", "--porcelain")
        assert "A" in discover().parent_of


class TestRemoveUndeletableWorktree:
    @pytest.mark.xfail(
        strict=True,
        reason="shutil.rmtree's OSError escapes main(), which catches only CalledProcessError and "
        "TreeError, so --json prints a Python traceback and no envelope after some of the tree is "
        "already deleted. Fix: catch OSError in _force_remove_worktree and raise TreeError, naming "
        "the worktree that could not be removed and which ones already were.",
    )
    def test_rmtree_failure_still_reports_an_envelope(
        self, repo: RepoHelper, tmp_path, capsys
    ) -> None:
        """Every other failure in agent mode arrives as one JSON envelope on stdout.

        `_force_remove_worktree` is the most destructive path in the tool and the only one that
        can fail after it has already started deleting, so a bare traceback here leaves an agent
        with no machine-readable account of a half-removed tree.
        """
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))
        sealed = wt / "sealed"
        sealed.mkdir()
        (sealed / "keep.txt").write_text("cannot be unlinked")
        sealed.chmod(0o500)  # readable and traversable, but its entries cannot be removed
        try:
            with pytest.raises(SystemExit):
                main(["remove", "A", "--json", "-y", "--force"])
            envelope = json.loads(capsys.readouterr().out)
            assert envelope["ok"] is False
        finally:
            sealed.chmod(0o700)


class TestRemoveUntrackedConfig:
    def test_untracked_work_blocks_removal_despite_status_config(
        self, repo: RepoHelper, tmp_path
    ) -> None:
        """`status.showUntrackedFiles=no` must not disarm the gate.

        It is a common large-repo perf setting, often global. The dirt gate already overrides the
        parallel `submodule.<n>.ignore` axis with `--ignore-submodules=none`; untracked files are
        the same idea, and `remove` force-deletes the directory, so a hidden untracked file is
        unrecoverable.
        """
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))
        repo.git("config", "status.showUntrackedFiles", "no")
        (wt / "notes.txt").write_text("UNTRACKED WORK")
        assert repo.git("status", "--porcelain", cwd=wt) == ""  # hidden from a bare status

        with pytest.raises(TreeError) as exc:
            cmd_remove(_ns(branch="A", yes=True))
        assert exc.value.code == 4
        assert (wt / "notes.txt").read_text() == "UNTRACKED WORK"
