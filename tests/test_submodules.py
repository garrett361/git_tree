from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from git_tree._errors import TreeError
from git_tree._git import (
    _check_submodule_health,
    _force_remove_worktree,
    _has_active_rebase,
    _submodule_paths,
)
from git_tree._graph import discover
from git_tree.cli import cmd_branch, cmd_propagate, cmd_rebase, cmd_rebuild, cmd_remove, main

from .conftest import (
    RepoHelper,
    _git,
    add_submodule,
    allow_file_protocol,
    cli_args,
    corrupt_submodule,
)


class TestRebuild:
    def _ns(self, branch: str | None = None, yes: bool = True, force: bool = False):
        return cli_args(branch=branch, yes=yes, force=force)

    def test_rebuild_recreates_worktree(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        allow_file_protocol(monkeypatch)

        add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)

        corrupt_submodule(wt, "mysub")

        cmd_rebuild(self._ns("child"))
        assert wt.exists()
        assert (wt / "mysub" / ".git").exists()
        assert "Initialize submodules" in capsys.readouterr().out

    def test_rebuild_rejects_non_tree_branch(self, repo: RepoHelper, capsys) -> None:
        with pytest.raises(TreeError):
            cmd_rebuild(self._ns("main"))
        assert "only acts on tree-branches" in capsys.readouterr().err

    def test_rebuild_rejects_no_worktree(self, repo: RepoHelper, capsys) -> None:
        repo.branch("orphan", parent="main")
        with pytest.raises(TreeError):
            cmd_rebuild(self._ns("orphan"))
        assert "has no worktree" in capsys.readouterr().err

    def test_rebuild_refuses_dirty_without_force(self, repo: RepoHelper, tmp_path, capsys) -> None:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        repo.dirty(cwd=wt)

        with pytest.raises(TreeError):
            cmd_rebuild(self._ns("child", force=False))
        assert "uncommitted changes" in capsys.readouterr().err

        cmd_rebuild(self._ns("child", force=True))
        assert wt.exists()
        assert not (wt / "dirty.txt").exists()

    def test_rebuild_preserves_tree_config(self, repo: RepoHelper, tmp_path, capsys) -> None:
        repo.branch("child", parent="main")
        repo.worktree("child", str(tmp_path / "wt-child"))

        cmd_rebuild(self._ns("child"))
        graph = discover()
        assert graph.parent_of["child"] == "main"
        out = capsys.readouterr().out
        assert "Recreate worktree" in out
        assert "Initialize submodules" not in out

    def test_rebuild_points_to_recovery_for_deleted_worktree_dir(
        self, repo: RepoHelper, tmp_path, capsys
    ) -> None:
        # A deleted worktree dir leaves a prunable registration that discover() drops, so the
        # branch looks worktree-less. rebuild can't recreate it in place; it must point the user
        # at recovery instead of the misleading "nothing to rebuild".
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        shutil.rmtree(wt)

        with pytest.raises(TreeError) as exc:
            cmd_rebuild(self._ns("child"))
        assert exc.value.code == 4
        err = capsys.readouterr().err
        assert "git worktree prune" in err
        assert "git worktree add" in err and "child" in err


class TestRemoveSubmodule:
    """`git tree remove` force-removes worktrees git itself refuses to remove when they contain
    submodules, gated on a recursive dirty check with a `--force` override."""

    def _child_with_submodule(self, repo: RepoHelper, tmp_path) -> Path:
        add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)
        assert (wt / "mysub" / ".git").exists()  # submodule initialized in the child worktree
        return wt

    def _ns(self, force: bool = False):
        return cli_args(branch="child", yes=True, force=force)

    def test_removes_clean_submodule_worktree(self, repo, tmp_path, monkeypatch) -> None:
        allow_file_protocol(monkeypatch)
        wt = self._child_with_submodule(repo, tmp_path)

        cmd_remove(self._ns())

        assert not wt.exists()  # the bug: pre-fix git refused and this raised
        assert repo.git("rev-parse", "--verify", "refs/heads/child")  # branch ref kept
        assert "child" not in discover().parent_of  # detached from the tree

    def test_dirty_submodule_refuses_without_force(
        self, repo, tmp_path, monkeypatch, capsys
    ) -> None:
        allow_file_protocol(monkeypatch)
        wt = self._child_with_submodule(repo, tmp_path)
        (wt / "mysub" / "dirty.txt").write_text("uncommitted submodule work")

        with pytest.raises(TreeError) as exc:
            cmd_remove(self._ns(force=False))
        assert exc.value.code == 4
        assert "uncommitted changes" in capsys.readouterr().err
        assert wt.exists()

    def test_dirty_submodule_refuses_even_with_ignore_config(
        self, repo, tmp_path, monkeypatch
    ) -> None:
        allow_file_protocol(monkeypatch)
        wt = self._child_with_submodule(repo, tmp_path)
        repo.git("config", "submodule.mysub.ignore", "all")  # would hide the dirt from plain status
        (wt / "mysub" / "dirty.txt").write_text("uncommitted")

        with pytest.raises(TreeError) as exc:
            cmd_remove(self._ns(force=False))  # --ignore-submodules=none overrides the config
        assert exc.value.code == 4
        assert wt.exists()

    def test_dirty_nested_submodule_refuses(self, repo, tmp_path, monkeypatch) -> None:
        allow_file_protocol(monkeypatch)
        # A submodule (mysub) that itself contains a submodule (deep).
        deep = tmp_path / "sub-deep"
        deep.mkdir()
        _git("init", cwd=deep)
        (deep / "d.txt").write_text("deep")
        _git("add", "d.txt", cwd=deep)
        _git("commit", "-m", "deep init", cwd=deep)
        mid = tmp_path / "sub-mid"
        mid.mkdir()
        _git("init", cwd=mid)
        _git("-c", "protocol.file.allow=always", "submodule", "add", str(deep), "deep", cwd=mid)
        _git("commit", "-m", "add deep", cwd=mid)
        repo.git("-c", "protocol.file.allow=always", "submodule", "add", str(mid), "mysub")
        repo.git("commit", "-m", "add mysub")
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)
        assert (wt / "mysub" / "deep" / ".git").exists()

        (wt / "mysub" / "deep" / "dirty.txt").write_text("uncommitted deep work")

        with pytest.raises(TreeError) as exc:
            cmd_remove(self._ns(force=False))  # foreach --recursive reaches the nested submodule
        assert exc.value.code == 4
        assert wt.exists()

    def test_dirty_submodule_removed_with_force(self, repo, tmp_path, monkeypatch, capsys) -> None:
        allow_file_protocol(monkeypatch)
        wt = self._child_with_submodule(repo, tmp_path)
        (wt / "mysub" / "dirty.txt").write_text("uncommitted")

        cmd_remove(self._ns(force=True))

        assert not wt.exists()
        assert "will destroy uncommitted changes" in capsys.readouterr().out

    def test_refuses_when_cwd_inside_worktree(self, repo, tmp_path, monkeypatch, capsys) -> None:
        # A mid-rebase worktree is detached (so the name-based "branch you're on" guard won't
        # fire) yet still tree-mapped, so it WOULD be force-removed out from under a cwd inside
        # it. The path-based cwd guard (which runs before the dirty gate) must stop that.
        repo.commit("shared.txt", "base", "base")
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / "shared.txt").write_text("child")
        repo.git("add", "shared.txt", cwd=wt)
        repo.git("commit", "-m", "child edits shared", cwd=wt)
        repo.git("branch", "other", "main")
        repo.checkout("other")
        repo.commit("shared.txt", "other", "other edits shared")
        repo.git("rebase", "other", cwd=wt, check=False)  # conflicts -> mid-rebase, detached
        assert _has_active_rebase(wt)
        monkeypatch.chdir(wt)

        with pytest.raises(TreeError) as exc:
            cmd_remove(self._ns())
        assert exc.value.code == 4
        assert "inside a worktree being removed" in capsys.readouterr().err
        assert wt.exists()


class TestBranchSubmoduleInit:
    def test_branch_inits_submodules(self, repo: RepoHelper, tmp_path, monkeypatch) -> None:
        allow_file_protocol(monkeypatch)

        add_submodule(repo, "mysub", tmp_path)

        wt_path = str(tmp_path / "wt-child")
        cmd_branch(cli_args(command="branch", name="child", path=wt_path))

        assert (Path(wt_path) / "mysub" / ".git").exists()
        assert (Path(wt_path) / "mysub" / "readme.txt").exists()

    def test_branch_no_submodule_init_flag(self, repo: RepoHelper, tmp_path, monkeypatch) -> None:
        allow_file_protocol(monkeypatch)

        add_submodule(repo, "mysub", tmp_path)

        wt_path = str(tmp_path / "wt-child")
        cmd_branch(cli_args(command="branch", name="child", path=wt_path, no_submodule_init=True))

        assert (Path(wt_path) / "mysub").exists()
        assert not (Path(wt_path) / "mysub" / "readme.txt").exists()


class TestPropagateSubmoduleHealth:
    def test_propagate_detects_unhealthy_submodule(
        self, repo: RepoHelper, tmp_path, capsys, monkeypatch
    ) -> None:
        allow_file_protocol(monkeypatch)

        add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)

        repo.checkout("main")
        repo.commit("extra.txt", "extra", "advance main")

        corrupt_submodule(wt, "mysub")

        with pytest.raises(TreeError):
            cmd_propagate(cli_args(dry_run=False, no_auto_rerere=False, branch=None, yes=True))
        err = capsys.readouterr().err
        assert "corrupted submodule state" in err
        assert "git tree rebuild" in err

    def test_submodule_health_checked_before_clean_state(
        self, repo: RepoHelper, tmp_path, capsys, monkeypatch
    ) -> None:
        """Ordering invariant: the submodule-health gate runs before the clean-state gate
        (git status crashes on a corrupted submodule). A worktree that is BOTH mid-rebase
        AND has a corrupted submodule must report the submodule problem, not the rebase."""
        allow_file_protocol(monkeypatch)

        add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)

        # Leave child's worktree mid-rebase (an unclean state the clean gate would flag).
        (wt / "z.txt").write_text("child")
        repo.git("add", "z.txt", cwd=wt)
        repo.git("commit", "-m", "child z", cwd=wt)
        repo.git("branch", "other", "main")
        repo.checkout("other")
        repo.commit("z.txt", "other", "other z")
        repo.git("rebase", "other", cwd=wt, check=False)  # conflicts, leaves rebase in progress
        assert _has_active_rebase(wt)

        corrupt_submodule(wt, "mysub")

        with pytest.raises(TreeError):
            cmd_propagate(cli_args(dry_run=False, no_auto_rerere=False, branch="main", yes=True))
        err = capsys.readouterr().err
        assert "corrupted submodule state" in err  # health gate won
        assert "rebase in progress" not in err  # clean gate never got to report

    def test_propagate_passes_with_uninitialized_submodules(
        self, repo: RepoHelper, tmp_path, monkeypatch
    ) -> None:
        """Uninitialized submodules (no .git) should NOT block propagate."""
        allow_file_protocol(monkeypatch)

        add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        repo.worktree("child", str(tmp_path / "wt-child"))

        repo.checkout("main")
        repo.commit("extra.txt", "extra", "advance main")

        cmd_propagate(cli_args(dry_run=False, no_auto_rerere=False, branch=None, yes=True))
        log = _git("log", "--oneline", "child", cwd=repo.work)
        assert "advance main" in log


class TestRebaseSubmoduleHealth:
    """Coverage gap #1: cmd_rebase health check (both branch and descendants)."""

    def test_rebase_detects_unhealthy_submodule_on_descendant(
        self, repo: RepoHelper, tmp_path, capsys, monkeypatch
    ) -> None:
        allow_file_protocol(monkeypatch)

        add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt_child = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt_child / "c.txt").write_text("c")
        _git("add", "c.txt", cwd=wt_child)
        _git("commit", "-m", "child work", cwd=wt_child)

        # Create grandchild
        _git("branch", "grandchild", cwd=wt_child)
        repo.set_parent("grandchild", "child")
        wt_grand = repo.worktree("grandchild", str(tmp_path / "wt-grand"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt_grand)
        (wt_grand / "g.txt").write_text("g")
        _git("add", "g.txt", cwd=wt_grand)
        _git("commit", "-m", "grand work", cwd=wt_grand)

        corrupt_submodule(wt_grand, "mysub")
        monkeypatch.chdir(wt_child)

        with pytest.raises(TreeError):
            cmd_rebase(
                cli_args(
                    command="rebase",
                    target="main",
                    dry_run=False,
                    no_auto_rerere=False,
                    yes=True,
                )
            )
        err = capsys.readouterr().err
        assert "corrupted submodule state" in err
        assert "grandchild" in err

    def test_rebase_detects_unhealthy_submodule_on_branch(
        self, repo: RepoHelper, tmp_path, capsys, monkeypatch
    ) -> None:
        # cmd_rebase health-checks the branch itself, a distinct call site from the
        # descendant check above (and one propagate never exercises).
        allow_file_protocol(monkeypatch)

        add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)
        (wt / "c.txt").write_text("c")
        _git("add", "c.txt", cwd=wt)
        _git("commit", "-m", "child work", cwd=wt)

        corrupt_submodule(wt, "mysub")
        monkeypatch.chdir(wt)

        with pytest.raises(TreeError):
            cmd_rebase(
                cli_args(
                    command="rebase",
                    target="main",
                    dry_run=False,
                    no_auto_rerere=False,
                    yes=True,
                )
            )
        err = capsys.readouterr().err
        assert "corrupted submodule state" in err


class TestCheckSubmoduleHealth:
    """Coverage gaps #2, #3, #9: unit tests for _check_submodule_health paths."""

    def test_healthy_git_directory(self, tmp_path) -> None:
        """Gap #2: .git is a directory containing HEAD (old-style layout)."""
        sub = tmp_path / "mysub"
        sub.mkdir()
        dot_git = sub / ".git"
        dot_git.mkdir()
        (dot_git / "HEAD").write_text("ref: refs/heads/main\n")

        assert _check_submodule_health(tmp_path, "mysub") is True

    def test_unhealthy_git_directory_missing_head(self, tmp_path) -> None:
        """Gap #2 inverse: .git directory exists but HEAD is missing."""
        sub = tmp_path / "mysub"
        sub.mkdir()
        (sub / ".git").mkdir()

        assert _check_submodule_health(tmp_path, "mysub") is False

    def test_healthy_relative_gitdir_pointer(self, tmp_path) -> None:
        """Gap #3: .git file with relative gitdir: path that resolves correctly."""
        sub = tmp_path / "mysub"
        sub.mkdir()
        # Simulate the real layout: gitdir points to ../../.git/modules/mysub
        modules_dir = tmp_path / ".git" / "modules" / "mysub"
        modules_dir.mkdir(parents=True)
        (modules_dir / "HEAD").write_text("ref: refs/heads/main\n")

        (sub / ".git").write_text("gitdir: ../.git/modules/mysub\n")

        assert _check_submodule_health(tmp_path, "mysub") is True

    def test_unhealthy_relative_gitdir_pointer(self, tmp_path) -> None:
        """Relative gitdir: resolves to a dir but HEAD is missing."""
        sub = tmp_path / "mysub"
        sub.mkdir()
        modules_dir = tmp_path / ".git" / "modules" / "mysub"
        modules_dir.mkdir(parents=True)
        # No HEAD file

        (sub / ".git").write_text("gitdir: ../.git/modules/mysub\n")

        assert _check_submodule_health(tmp_path, "mysub") is False

    def test_uninitialized_submodule_is_healthy(self, tmp_path) -> None:
        """No .git at all — treated as uninitialized, not corrupted."""
        sub = tmp_path / "mysub"
        sub.mkdir()

        assert _check_submodule_health(tmp_path, "mysub") is True

    def test_unreadable_git_file(self, tmp_path) -> None:
        """Gap #9: .git file exists but is unreadable (OSError)."""
        sub = tmp_path / "mysub"
        sub.mkdir()
        dot_git = sub / ".git"
        dot_git.write_text("gitdir: somewhere\n")
        os.chmod(str(dot_git), 0o000)

        try:
            assert _check_submodule_health(tmp_path, "mysub") is False
        finally:
            os.chmod(str(dot_git), 0o644)

    def test_git_file_no_gitdir_prefix(self, tmp_path) -> None:
        """.git file exists but content doesn't start with 'gitdir: '."""
        sub = tmp_path / "mysub"
        sub.mkdir()
        (sub / ".git").write_text("garbage content\n")

        assert _check_submodule_health(tmp_path, "mysub") is False


class TestSubmodulePaths:
    """Coverage gaps #7, #8: _submodule_paths edge cases."""

    def test_no_gitmodules_file(self, tmp_path) -> None:
        """Gap #8: worktree has no .gitmodules — returns empty list."""
        assert _submodule_paths(tmp_path) == []

    def test_filters_nonexistent_paths(self, tmp_path) -> None:
        """Gap #7: .gitmodules lists paths that don't exist on disk."""
        (tmp_path / ".gitmodules").write_text(
            '[submodule "exists"]\n'
            "    path = exists\n"
            "    url = https://example.com/exists.git\n"
            '[submodule "ghost"]\n'
            "    path = ghost\n"
            "    url = https://example.com/ghost.git\n"
        )
        (tmp_path / "exists").mkdir()
        # "ghost" dir intentionally not created

        result = _submodule_paths(tmp_path)
        assert result == ["exists"]

    def test_handles_percent_in_paths(self, tmp_path) -> None:
        """Regression: configparser with interpolation would crash on % chars."""
        (tmp_path / ".gitmodules").write_text(
            '[submodule "weird"]\n    path = 100%done\n    url = https://example.com/weird.git\n'
        )
        (tmp_path / "100%done").mkdir()

        result = _submodule_paths(tmp_path)
        assert result == ["100%done"]


class TestForceRemoveWorktree:
    """Coverage gaps #4, #5: _force_remove_worktree fallback and failure paths."""

    def test_fallback_rmtree_when_git_remove_fails(self, repo: RepoHelper, tmp_path) -> None:
        """Gap #4: stage-1 fails, falls through to rmtree + prune."""
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))

        # Corrupt the worktree's .git file so `git worktree remove --force` fails
        (wt / ".git").unlink()
        (wt / ".git").write_text("garbage\n")

        _force_remove_worktree(wt, "child")
        assert not wt.exists()

    def test_raises_when_worktree_still_registered(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        """Gap #5: worktree remains in git's list after all stages — raises TreeError."""
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))

        # Fail stage 1 and neuter stage 2, so the registration survives to the verify stage.
        import git_tree._git as git_mod

        original_git_echo_ok = git_mod.git_echo_ok

        def fake_git_echo_ok(*args, **kwargs):
            if args and args[0] == "worktree" and len(args) > 1 and args[1] == "remove":
                return False
            return original_git_echo_ok(*args, **kwargs)

        monkeypatch.setattr(git_mod, "git_echo_ok", fake_git_echo_ok)
        monkeypatch.setattr(shutil, "rmtree", lambda p: None)  # Don't actually delete

        with pytest.raises(TreeError):
            _force_remove_worktree(wt, "child")
        assert "Could not fully deregister" in capsys.readouterr().err


class TestRebuildCwdGuard:
    """Coverage gap #6: cmd_rebuild refuses when cwd is inside target worktree."""

    def test_rebuild_refuses_from_subdirectory_of_worktree(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        subdir = wt / "deep" / "nested"
        subdir.mkdir(parents=True)

        monkeypatch.chdir(subdir)

        with pytest.raises(TreeError):
            cmd_rebuild(cli_args(branch="child", yes=True, force=False))
        err = capsys.readouterr().err
        assert "inside its worktree" in err


class TestRebuildCorruptedGitStatus:
    """A worktree rebuild cannot inspect is one it refuses to delete without --force."""

    def test_rebuild_refuses_when_git_status_crashes(self, repo: RepoHelper, tmp_path) -> None:
        """A worktree too broken to inspect is not a worktree that is safe to delete.

        Rebuild removes the directory outright, so "cannot prove it is clean" must not read as
        "is clean", which is the posture `remove` takes. `--force` is the way through, once the user
        has rescued anything they need.
        """
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / "keepme.txt").write_text("uncommitted work git status cannot report")

        # Corrupt so git status fails (CalledProcessError)
        (wt / ".git").unlink()
        (wt / ".git").write_text("garbage\n")

        with pytest.raises(TreeError) as exc:
            cmd_rebuild(cli_args(branch="child", yes=True, force=False))
        assert exc.value.code == 4
        assert (wt / "keepme.txt").exists()

    def test_rebuild_force_proceeds_when_git_status_crashes(
        self, repo: RepoHelper, tmp_path
    ) -> None:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / ".git").unlink()
        (wt / ".git").write_text("garbage\n")

        cmd_rebuild(cli_args(branch="child", yes=True, force=True))
        assert wt.exists()
        assert (wt / "init.txt").exists()


class TestSubmoduleInitFailure:
    """Init failures must be honest: rebuild (whose job is submodule health) errors; branch
    (which created the worktree successfully) warns but succeeds."""

    def _submodule_on_main(self, repo: RepoHelper, tmp_path, monkeypatch) -> None:
        allow_file_protocol(monkeypatch)
        add_submodule(repo, "mysub", tmp_path)

    def test_rebuild_errors_when_submodule_init_fails(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        self._submodule_on_main(repo, tmp_path, monkeypatch)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")  # block the re-init's file:// clone

        with pytest.raises(TreeError) as exc:
            cmd_rebuild(cli_args(branch="child", yes=True, force=True))
        assert exc.value.code == 4
        captured = capsys.readouterr()
        assert "submodule init failed" in captured.err
        assert "is healthy" not in captured.out  # never claims health it didn't verify
        assert wt.exists()  # the worktree was still recreated

    def test_branch_warns_but_succeeds_when_submodule_init_fails(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        self._submodule_on_main(repo, tmp_path, monkeypatch)
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")  # block the init's file:// clone

        wt_path = str(tmp_path / "wt-child")
        cmd_branch(cli_args(command="branch", name="child", path=wt_path))
        captured = capsys.readouterr()
        assert "Warning: submodule init did not complete" in captured.err
        assert "Created branch child" in captured.out  # branch creation still succeeded
        assert Path(wt_path).exists()


class TestUnparseableGitmodules:
    """git accepts a repeated `[submodule "x"]` section; configparser does not.

    Left uncaught it escaped `main()`'s handlers, giving a traceback and no JSON envelope. It
    must also fail closed in the removal gate: a `.gitmodules` we cannot read is not proof that
    there is no submodule work to lose.
    """

    def _repo_with_bad_gitmodules(self, repo: RepoHelper, tmp_path) -> Path:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / ".gitmodules").write_text(
            '[submodule "x"]\n\tpath = x\n[submodule "x"]\n\tpath = x\n'
        )
        repo.git("add", ".gitmodules", cwd=wt)
        repo.git("commit", "-m", "duplicate submodule sections", cwd=wt)
        return wt

    def test_remove_refuses_rather_than_crashing(self, repo: RepoHelper, tmp_path) -> None:
        wt = self._repo_with_bad_gitmodules(repo, tmp_path)
        with pytest.raises(TreeError) as exc:
            cmd_remove(cli_args(branch="child", yes=True))
        assert exc.value.code == 4
        # It has to be the removal gate refusing, not `_submodule_paths`' own error escaping
        # through it: only the gate names the branch, and only the gate fails closed by design.
        assert exc.value.branches == ["child"]
        assert wt.exists()

    def test_json_mode_reports_an_envelope(self, repo: RepoHelper, tmp_path, capsys) -> None:
        """The parse error used to escape `main()` entirely, giving a traceback and no envelope."""
        self._repo_with_bad_gitmodules(repo, tmp_path)
        with pytest.raises(SystemExit):
            main(["remove", "child", "--json", "-y"])

        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == 4

    @pytest.mark.xfail(
        strict=True,
        reason="_remove_blocking_dirt returns one bool for four situations (real dirt, unreadable "
        ".gitmodules, an uninspectable submodule directory, a failed foreach), so cmd_remove and "
        "cmd_rebuild describe all four as uncommitted changes and tell the user to --force away "
        "work that does not exist. Fix: return a reason instead, a StrEnum kind plus git's own "
        "text as detail, and word the refusal per kind at each call site. Doing this also fixes "
        "test_force_removes_a_worktree_with_a_corrupted_submodule, since the gate stops raising.",
    )
    def test_refusal_names_the_real_cause(self, repo: RepoHelper, tmp_path) -> None:
        """Failing closed is right; describing it as dirt is not.

        The parse error reaches stderr only because `TreeError.__init__` prints on construction,
        and never reaches the envelope at all. An agent reading the message is told to re-run with
        `--force` "to discard uncommitted work" that does not exist.
        """
        self._repo_with_bad_gitmodules(repo, tmp_path)
        with pytest.raises(TreeError) as exc:
            cmd_remove(cli_args(branch="child", yes=True))

        assert ".gitmodules" in exc.value.message
        assert "uncommitted changes" not in exc.value.message


class TestRemoveForceOnCorruptedSubmodule:
    @pytest.mark.xfail(
        strict=True,
        reason="cmd_remove runs _remove_blocking_dirt even when force is already true, only to "
        "build a warning banner, and its `git status --ignore-submodules=none` exits 128 on a "
        "dangling submodule pointer. The uncaught CalledProcessError means --force cannot remove "
        "the worktree it exists for, leaving `rm -rf` plus a manual prune as the only way out. "
        "Fix: make the gate total so it reports 'cannot prove clean' rather than raising, per "
        "test_refusal_names_the_real_cause; guarding the call with `if not force` would work but "
        "loses the banner.",
    )
    def test_force_removes_a_worktree_with_a_corrupted_submodule(
        self, repo: RepoHelper, tmp_path, monkeypatch
    ) -> None:
        """`--force` is the escape hatch, and it cannot open on the case that needs it most.

        `_remove_blocking_dirt` runs `git status --ignore-submodules=none`, which exits 128 when a
        submodule's `.git` pointer dangles. Nothing catches that, so both plain and `--force`
        removal die with a raw git failure and the only way out is `rm -rf` plus a manual prune.
        """
        allow_file_protocol(monkeypatch)
        add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)
        corrupt_submodule(wt, "mysub")

        cmd_remove(cli_args(branch="child", yes=True, force=True))

        assert not wt.exists()
        assert "child" not in discover().parent_of
