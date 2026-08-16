from __future__ import annotations

from pathlib import Path

import pytest

from git_tree._cmd_rebuild import cmd_rebuild
from git_tree._errors import TreeError
from git_tree._git import _has_active_rebase
from git_tree._graph import discover

from .conftest import RepoHelper, add_submodule, allow_file_protocol, cli_args, stopped_rebase


def _ns(branch: str, force: bool = False) -> object:
    return cli_args(branch=branch, yes=True, force=force)


class TestRebuildMidRebase:
    """Rebuild deletes and recreates the worktree, which discards an in-progress rebase.

    The branch ref does not move until a rebase finishes, so every conflict already resolved in
    it lives only on this worktree's detached HEAD and in its own HEAD reflog. Both go with the
    directory, and no reflog path leads back.
    """

    def test_refuses_and_leaves_the_rebase_intact(self, repo: RepoHelper, tmp_path) -> None:
        wt = stopped_rebase(repo, tmp_path)
        with pytest.raises(TreeError) as exc:
            cmd_rebuild(_ns("A"))

        assert exc.value.code == 4
        # Pin the gate that fired: cmd_rebuild has eight code-4 raises, so the code alone would
        # also be satisfied by the dirt gate or the unreadable-status gate refusing instead.
        assert "Rebuilding discards it" in exc.value.message
        assert _has_active_rebase(wt)
        assert "A" in discover().parent_of

    def test_force_discards_it(self, repo: RepoHelper, tmp_path) -> None:
        wt = stopped_rebase(repo, tmp_path)
        cmd_rebuild(_ns("A", force=True))

        assert not _has_active_rebase(wt)
        # A rebuild that deleted the directory and never repopulated it would also satisfy the
        # assertion above, so check the worktree came back with the branch checked out.
        assert wt.exists()
        assert (wt / "shared.txt").exists()
        assert repo.git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "A"

    @pytest.mark.xfail(
        strict=True,
        reason="rebuild's --force waives two independent gates, dirt and mid-rebase, and prints "
        "no warning for either, while its plan ('Remove corrupted worktree / Recreate worktree') "
        "reads as a repair. Someone forcing past scratch edits also silently loses a paused "
        "rebase and every conflict resolved in it. cmd_remove already prints a per-branch banner "
        "for each ('--force will destroy uncommitted changes...', '--force will discard the "
        "rebase in progress in:'). Fix: print both in cmd_rebuild before the confirm, reusing "
        "that wording. Related, not asserted here: rebuild has no --dry-run, so the banner is the "
        "only preview there can be.",
    )
    def test_force_warns_before_discarding_the_rebase(
        self, repo: RepoHelper, tmp_path, capsys
    ) -> None:
        """`--force` is one flag over two risks, so it must say which one it is acting on.

        The branch ref has not moved, so the resolved conflicts exist only in this worktree.
        Destroying them silently, under a plan that reads as a repair, is the whole problem.
        """
        stopped_rebase(repo, tmp_path)

        cmd_rebuild(_ns("A", force=True))

        assert "discard the rebase in progress" in capsys.readouterr().out


class TestRebuildDirtGate:
    """Rebuild uses the same gate as remove. A bare `git status` is not enough: it honours
    `.gitmodules` `ignore` settings and never reports a populated-but-uninitialized submodule."""

    def _child_with_submodule(self, repo: RepoHelper, tmp_path: Path) -> Path:
        add_submodule(repo, "sub", tmp_path)
        repo.branch("child", parent="main")
        return repo.worktree("child", str(tmp_path / "wt-child"))

    def test_refuses_submodule_dirt_hidden_by_ignore_all(
        self, repo: RepoHelper, tmp_path, monkeypatch
    ) -> None:
        allow_file_protocol(monkeypatch)
        wt = self._child_with_submodule(repo, tmp_path)
        repo.git("-c", "protocol.file.allow=always", "submodule", "update", "--init", cwd=wt)
        # Commit `ignore = all` on the child itself, the way an upstream .gitmodules would carry it.
        repo.git("config", "-f", str(wt / ".gitmodules"), "submodule.sub.ignore", "all")
        repo.git("commit", "-am", "ignore submodule changes", cwd=wt)

        (wt / "sub" / "readme.txt").write_text("PRECIOUS UNCOMMITTED WORK")
        assert repo.git("status", "--porcelain", cwd=wt) == ""  # invisible to a bare status

        with pytest.raises(TreeError) as exc:
            cmd_rebuild(_ns("child"))
        assert exc.value.code == 4
        assert "Pass --force to rebuild anyway" in exc.value.message  # the dirt gate, not another
        assert (wt / "sub" / "readme.txt").read_text() == "PRECIOUS UNCOMMITTED WORK"

    def test_refuses_populated_uninitialized_submodule(
        self, repo: RepoHelper, tmp_path, monkeypatch
    ) -> None:
        allow_file_protocol(monkeypatch)
        wt = self._child_with_submodule(repo, tmp_path)
        (wt / "sub").mkdir(exist_ok=True)
        (wt / "sub" / "notes.txt").write_text("NOTES")
        assert repo.git("status", "--porcelain", cwd=wt) == ""

        with pytest.raises(TreeError) as exc:
            cmd_rebuild(_ns("child"))
        assert exc.value.code == 4
        assert "Pass --force to rebuild anyway" in exc.value.message  # the dirt gate, not another
        assert (wt / "sub" / "notes.txt").read_text() == "NOTES"


class TestRebuildIgnoredFiles:
    @pytest.mark.xfail(
        strict=True,
        reason="rebuild deletes the worktree directory without saying git-ignored files go with "
        "it. Fix: print the notice cmd_remove prints, on every path, not only under --force.",
    )
    def test_plan_discloses_that_ignored_files_are_deleted(
        self, repo: RepoHelper, tmp_path, capsys
    ) -> None:
        """`remove` says this and `rebuild` does not, though both delete the same directory.

        `git status` never reports ignored files, so they are never counted as dirt and the gate
        that would refuse never sees them. A `.env` or a virtualenv is destroyed by a command
        whose printed plan reads as a repair.
        """
        repo.commit(".gitignore", ".env\n", "ignore .env")
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / ".env").write_text("SECRET=hunter2")
        assert repo.git("status", "--porcelain", cwd=wt) == ""  # invisible to the dirt gate

        cmd_rebuild(_ns("child"))

        assert not (wt / ".env").exists()  # destroyed, as the plan should have said
        assert "git-ignored" in capsys.readouterr().out
