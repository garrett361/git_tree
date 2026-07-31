from __future__ import annotations

from pathlib import Path

import pytest

from git_tree.cli import TreeError, _has_active_rebase, cmd_rebuild, discover

from .conftest import RepoHelper, _git, cli_args


def _ns(branch: str, force: bool = False) -> object:
    return cli_args(branch=branch, yes=True, force=force)


def _submodule(repo: RepoHelper, name: str, tmp_path: Path) -> None:
    sub = tmp_path / f"sub-{name}"
    sub.mkdir()
    _git("init", cwd=sub)
    _git("config", "user.email", "test@test.com", cwd=sub)
    _git("config", "user.name", "Test", cwd=sub)
    (sub / "readme.txt").write_text("sub content")
    _git("add", "readme.txt", cwd=sub)
    _git("commit", "-m", "sub init", cwd=sub)
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", str(sub), name)
    repo.git("commit", "-m", f"add submodule {name}")


class TestRebuildMidRebase:
    """Rebuild deletes and recreates the worktree, which discards an in-progress rebase.

    The branch ref does not move until a rebase finishes, so every conflict already resolved in
    it lives only on this worktree's detached HEAD and in its own HEAD reflog. Both go with the
    directory, and no reflog path leads back.
    """

    def _stopped(self, repo: RepoHelper, tmp_path: Path) -> Path:
        repo.commit("shared.txt", "base", "base shared")
        repo.branch("A", parent="main")
        wt = repo.worktree("A", str(tmp_path / "wt-A"))
        (wt / "shared.txt").write_text("A version")
        repo.git("add", "shared.txt", cwd=wt)
        repo.git("commit", "-m", "A edits shared", cwd=wt)
        repo.checkout("main")
        repo.commit("shared.txt", "main version", "main edits shared")
        repo.stop_rebase_clean(wt, "main", "shared.txt")
        return wt

    def test_refuses_and_leaves_the_rebase_intact(self, repo: RepoHelper, tmp_path) -> None:
        wt = self._stopped(repo, tmp_path)
        with pytest.raises(TreeError) as exc:
            cmd_rebuild(_ns("A"))

        assert exc.value.code == 4
        assert _has_active_rebase(wt)
        assert "A" in discover().parent_of

    def test_force_discards_it(self, repo: RepoHelper, tmp_path) -> None:
        wt = self._stopped(repo, tmp_path)
        cmd_rebuild(_ns("A", force=True))
        assert not _has_active_rebase(wt)


class TestRebuildDirtGate:
    """Rebuild uses the same gate as remove. A bare `git status` is not enough: it honours
    `.gitmodules` `ignore` settings and never reports a populated-but-uninitialized submodule."""

    def _child_with_submodule(self, repo: RepoHelper, tmp_path: Path) -> Path:
        _submodule(repo, "sub", tmp_path)
        repo.branch("child", parent="main")
        return repo.worktree("child", str(tmp_path / "wt-child"))

    def test_refuses_submodule_dirt_hidden_by_ignore_all(
        self, repo: RepoHelper, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
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
        assert (wt / "sub" / "readme.txt").read_text() == "PRECIOUS UNCOMMITTED WORK"

    def test_refuses_populated_uninitialized_submodule(
        self, repo: RepoHelper, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
        wt = self._child_with_submodule(repo, tmp_path)
        (wt / "sub").mkdir(exist_ok=True)
        (wt / "sub" / "notes.txt").write_text("NOTES")
        assert repo.git("status", "--porcelain", cwd=wt) == ""

        with pytest.raises(TreeError) as exc:
            cmd_rebuild(_ns("child"))
        assert exc.value.code == 4
        assert (wt / "sub" / "notes.txt").read_text() == "NOTES"
