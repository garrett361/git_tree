from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

import pytest

from git_tree.cli import (
    TreeError,
    _check_submodule_health,
    _force_remove_worktree,
    _submodule_paths,
    cmd_branch,
    cmd_propagate,
    cmd_rebase,
    cmd_repair,
    discover,
)

from .conftest import RepoHelper, _git


def _add_submodule(repo: RepoHelper, name: str, tmp_path: Path) -> Path:
    """Create a sub-repo and add it as a submodule. Returns submodule path in worktree."""
    sub_repo = tmp_path / f"sub-{name}"
    sub_repo.mkdir()
    _git("init", cwd=sub_repo)
    _git("config", "user.email", "test@test.com", cwd=sub_repo)
    _git("config", "user.name", "Test", cwd=sub_repo)
    (sub_repo / "readme.txt").write_text("sub content")
    _git("add", "readme.txt", cwd=sub_repo)
    _git("commit", "-m", "sub init", cwd=sub_repo)
    # Allow file:// transport for submodule clone
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", str(sub_repo), name)
    repo.git("commit", "-m", f"add submodule {name}")
    return repo.work / name


def _corrupt_submodule(worktree: Path, submodule_path: str) -> None:
    """Corrupt a submodule's .git pointer so health check fails."""
    dot_git = worktree / submodule_path / ".git"
    dot_git.write_text("gitdir: /nonexistent/path/that/does/not/exist\n")


class TestRepair:
    def _ns(self, branch: str | None = None, yes: bool = True, force: bool = False):
        return argparse.Namespace(branch=branch, yes=yes, force=force)

    def test_repair_recreates_worktree(self, repo: RepoHelper, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)

        _corrupt_submodule(wt, "mysub")

        cmd_repair(self._ns("child"))
        assert wt.exists()
        assert (wt / "mysub" / ".git").exists()

    def test_repair_handles_corrupted_worktree_contents(self, repo: RepoHelper, tmp_path) -> None:
        """Worktree directory exists but internals are broken (e.g. .git file deleted)."""
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))

        # Corrupt the worktree by removing its .git file (makes git status fail)
        (wt / ".git").unlink()
        (wt / ".git").write_text("garbage\n")

        cmd_repair(self._ns("child", force=True))
        assert wt.exists()
        assert (wt / "init.txt").exists()

    def test_repair_rejects_non_tree_branch(self, repo: RepoHelper, capsys) -> None:
        with pytest.raises(TreeError):
            cmd_repair(self._ns("main"))
        assert "not a repairable tree-branch" in capsys.readouterr().err

    def test_repair_rejects_no_worktree(self, repo: RepoHelper, capsys) -> None:
        repo.branch("orphan", parent="main")
        with pytest.raises(TreeError):
            cmd_repair(self._ns("orphan"))
        assert "has no worktree" in capsys.readouterr().err

    def test_repair_refuses_dirty_without_force(self, repo: RepoHelper, tmp_path, capsys) -> None:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        repo.dirty(cwd=wt)

        with pytest.raises(TreeError):
            cmd_repair(self._ns("child", force=False))
        assert "uncommitted changes" in capsys.readouterr().err

    def test_repair_allows_dirty_with_force(self, repo: RepoHelper, tmp_path) -> None:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        repo.dirty(cwd=wt)

        cmd_repair(self._ns("child", force=True))
        assert wt.exists()
        assert not (wt / "dirty.txt").exists()

    def test_repair_preserves_tree_config(self, repo: RepoHelper, tmp_path) -> None:
        repo.branch("child", parent="main")
        repo.worktree("child", str(tmp_path / "wt-child"))

        cmd_repair(self._ns("child"))
        graph = discover()
        assert graph.parent_of["child"] == "main"

    def test_repair_points_to_recovery_for_deleted_worktree_dir(
        self, repo: RepoHelper, tmp_path, capsys
    ) -> None:
        # A deleted worktree dir leaves a prunable registration that discover() drops, so the
        # branch looks worktree-less. repair can't rebuild it in place; it must point the user
        # at recovery instead of the misleading "nothing to repair".
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        shutil.rmtree(wt)

        with pytest.raises(TreeError) as exc:
            cmd_repair(self._ns("child"))
        assert exc.value.code == 4
        err = capsys.readouterr().err
        assert "git worktree prune" in err
        assert "git worktree add" in err and "child" in err

    def test_repair_omits_submodule_step_without_submodules(
        self, repo: RepoHelper, tmp_path, capsys
    ) -> None:
        repo.branch("child", parent="main")
        repo.worktree("child", str(tmp_path / "wt-child"))

        cmd_repair(self._ns("child"))
        out = capsys.readouterr().out
        assert "Recreate worktree" in out
        assert "Initialize submodules" not in out

    def test_repair_shows_submodule_step_with_submodules(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        repo.worktree("child", str(tmp_path / "wt-child"))

        cmd_repair(self._ns("child"))
        assert "Initialize submodules" in capsys.readouterr().out


class TestBranchSubmoduleInit:
    def test_branch_inits_submodules(self, repo: RepoHelper, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)

        wt_path = str(tmp_path / "wt-child")
        cmd_branch(argparse.Namespace(command="branch", name="child", path=wt_path))

        assert (Path(wt_path) / "mysub" / ".git").exists()
        assert (Path(wt_path) / "mysub" / "readme.txt").exists()

    def test_branch_no_submodule_init_flag(self, repo: RepoHelper, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)

        wt_path = str(tmp_path / "wt-child")
        cmd_branch(
            argparse.Namespace(command="branch", name="child", path=wt_path, no_submodule_init=True)
        )

        assert (Path(wt_path) / "mysub").exists()
        assert not (Path(wt_path) / "mysub" / "readme.txt").exists()


class TestPropagateSubmoduleHealth:
    def test_propagate_detects_unhealthy_submodule(
        self, repo: RepoHelper, tmp_path, capsys, monkeypatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)

        repo.checkout("main")
        repo.commit("extra.txt", "extra", "advance main")

        _corrupt_submodule(wt, "mysub")

        with pytest.raises(TreeError):
            cmd_propagate(
                argparse.Namespace(dry_run=False, no_auto_rerere=False, branch=None, yes=True)
            )
        err = capsys.readouterr().err
        assert "corrupted submodule state" in err

    def test_propagate_passes_with_uninitialized_submodules(
        self, repo: RepoHelper, tmp_path, monkeypatch
    ) -> None:
        """Uninitialized submodules (no .git) should NOT block propagate."""
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        repo.worktree("child", str(tmp_path / "wt-child"))

        repo.checkout("main")
        repo.commit("extra.txt", "extra", "advance main")

        cmd_propagate(
            argparse.Namespace(dry_run=False, no_auto_rerere=False, branch=None, yes=True)
        )
        log = _git("log", "--oneline", "child", cwd=repo.work)
        assert "advance main" in log

    def test_propagate_suggests_repair(
        self, repo: RepoHelper, tmp_path, capsys, monkeypatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)

        repo.checkout("main")
        repo.commit("extra.txt", "extra", "advance main")

        _corrupt_submodule(wt, "mysub")

        with pytest.raises(TreeError):
            cmd_propagate(
                argparse.Namespace(dry_run=False, no_auto_rerere=False, branch=None, yes=True)
            )
        err = capsys.readouterr().err
        assert "git tree repair" in err


class TestRebaseSubmoduleHealth:
    """Coverage gap #1: cmd_rebase health check (both branch and descendants)."""

    def test_rebase_detects_unhealthy_submodule_on_branch(
        self, repo: RepoHelper, tmp_path, capsys, monkeypatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        _git("submodule", "update", "--init", "--recursive", cwd=wt)
        (wt / "c.txt").write_text("c")
        _git("add", "c.txt", cwd=wt)
        _git("commit", "-m", "child work", cwd=wt)

        _corrupt_submodule(wt, "mysub")
        monkeypatch.chdir(wt)

        with pytest.raises(TreeError):
            cmd_rebase(
                argparse.Namespace(
                    command="rebase",
                    target="main",
                    dry_run=False,
                    no_auto_rerere=False,
                    yes=True,
                )
            )
        err = capsys.readouterr().err
        assert "corrupted submodule state" in err

    def test_rebase_detects_unhealthy_submodule_on_descendant(
        self, repo: RepoHelper, tmp_path, capsys, monkeypatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")

        _add_submodule(repo, "mysub", tmp_path)
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

        _corrupt_submodule(wt_grand, "mysub")
        monkeypatch.chdir(wt_child)

        with pytest.raises(TreeError):
            cmd_rebase(
                argparse.Namespace(
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

        # Patch git_echo_ok to fake stage-1 failure, and git_echo/git to fake prune
        # not cleaning up. Easiest: make rmtree not actually remove the .git/worktrees entry.
        # Instead, monkeypatch shutil.rmtree to be a no-op and git worktree remove to fail.
        import git_tree.cli as cli_mod

        original_git_echo_ok = cli_mod.git_echo_ok

        def fake_git_echo_ok(*args, **kwargs):
            if args and args[0] == "worktree" and len(args) > 1 and args[1] == "remove":
                return False
            return original_git_echo_ok(*args, **kwargs)

        monkeypatch.setattr(cli_mod, "git_echo_ok", fake_git_echo_ok)
        monkeypatch.setattr(shutil, "rmtree", lambda p: None)  # Don't actually delete

        with pytest.raises(TreeError):
            _force_remove_worktree(wt, "child")
        assert "Could not fully deregister" in capsys.readouterr().err


class TestRepairCwdGuard:
    """Coverage gap #6: cmd_repair refuses when cwd is inside target worktree."""

    def test_repair_refuses_when_cwd_inside_worktree(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))

        monkeypatch.chdir(wt)

        with pytest.raises(TreeError):
            cmd_repair(argparse.Namespace(branch="child", yes=True, force=False))
        err = capsys.readouterr().err
        assert "inside its worktree" in err

    def test_repair_refuses_from_subdirectory_of_worktree(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        subdir = wt / "deep" / "nested"
        subdir.mkdir(parents=True)

        monkeypatch.chdir(subdir)

        with pytest.raises(TreeError):
            cmd_repair(argparse.Namespace(branch="child", yes=True, force=False))
        err = capsys.readouterr().err
        assert "inside its worktree" in err


class TestRepairCorruptedGitStatus:
    """Coverage gap #10: cmd_repair proceeds without --force when git status crashes."""

    def test_repair_proceeds_when_git_status_crashes(self, repo: RepoHelper, tmp_path) -> None:
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))

        # Corrupt so git status fails (CalledProcessError)
        (wt / ".git").unlink()
        (wt / ".git").write_text("garbage\n")

        # Should succeed without --force since git status crash is caught
        cmd_repair(argparse.Namespace(branch="child", yes=True, force=False))
        assert wt.exists()
        assert (wt / "init.txt").exists()


class TestSubmoduleInitFailure:
    """Init failures must be honest: repair (whose job is submodule health) errors; branch
    (which created the worktree successfully) warns but succeeds."""

    def _submodule_on_main(self, repo: RepoHelper, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
        _add_submodule(repo, "mysub", tmp_path)

    def test_repair_errors_when_submodule_init_fails(
        self, repo: RepoHelper, tmp_path, monkeypatch, capsys
    ) -> None:
        self._submodule_on_main(repo, tmp_path, monkeypatch)
        repo.branch("child", parent="main")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")  # block the re-init's file:// clone

        with pytest.raises(TreeError) as exc:
            cmd_repair(argparse.Namespace(branch="child", yes=True, force=True))
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
        cmd_branch(argparse.Namespace(command="branch", name="child", path=wt_path))
        captured = capsys.readouterr()
        assert "Warning: submodule init did not complete" in captured.err
        assert "Created branch child" in captured.out  # branch creation still succeeded
        assert Path(wt_path).exists()
