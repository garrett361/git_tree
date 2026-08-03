from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

FZF_SELECT = "git_tree._prompt.fzf_select"
"""Monkeypatch target for the fzf picker, named once so tests don't each hard-code the module
a production function happens to live in."""


def cli_args(**overrides: object) -> argparse.Namespace:
    """A complete git-tree args namespace, every flag at its parser default, for calling `cmd_*`
    handlers directly. Built by walking the real parser, so it stays in sync with the command
    surface and production code never needs `getattr` fallbacks to tolerate partial fixtures."""
    from git_tree.cli import _build_parser

    parser = _build_parser()
    actions = list(parser._actions)
    sub = next((a for a in actions if isinstance(a, argparse._SubParsersAction)), None)
    if sub is not None:
        for subparser in sub.choices.values():
            actions.extend(subparser._actions)
    defaults: dict[str, object] = {}
    for action in actions:
        if action.dest == "help" or action.default is argparse.SUPPRESS:
            continue
        defaults.setdefault(action.dest, action.default)
    return argparse.Namespace(**{**defaults, **overrides})


def _git(*args: str, cwd: Path, check: bool = True, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )
    return result.stdout.strip()


@dataclass
class RepoHelper:
    work: Path
    origin: Path

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        return _git(*args, cwd=cwd or self.work, check=check)

    def commit(
        self, filename: str = "f.txt", content: str | None = None, message: str | None = None
    ) -> str:
        if content is None:
            content = filename
        (self.work / filename).parent.mkdir(parents=True, exist_ok=True)
        (self.work / filename).write_text(content)
        self.git("add", filename)
        self.git("commit", "-m", message or f"add {filename}")
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str, parent: str | None = None) -> None:
        self.git("branch", name)
        if parent:
            self.set_parent(name, parent)

    def checkout(self, name: str) -> None:
        self.git("checkout", name)

    def set_parent(self, branch: str, parent: str) -> None:
        self.git("config", f"branch.{branch}.tree-parent-branch", parent)
        fork = self.git("merge-base", parent, branch)
        self.git("config", f"branch.{branch}.tree-fork-commit", fork)

    def worktree(self, branch: str, path: str | None = None) -> Path:
        wt_path = self.work.parent / (path or f"wt-{branch}")
        self.git("worktree", "add", str(wt_path), branch)
        return wt_path

    def dirty(
        self, filename: str = "dirty.txt", content: str = "dirty", cwd: Path | None = None
    ) -> None:
        target = cwd or self.work
        (target / filename).write_text(content)

    @property
    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def log_oneline(self, ref: str = "HEAD") -> list[str]:
        out = self.git("log", "--oneline", ref)
        return out.splitlines() if out else []

    def push(self, branch: str) -> None:
        self.git("push", "-u", "origin", branch)

    def enable_rerere(self) -> None:
        self.git("config", "rerere.enabled", "true")
        self.git("config", "core.editor", "true")

    def rebase_interactive(self, worktree: Path, onto: str, verb: str) -> None:
        """Hand-run `git rebase -i <onto>` in `worktree`, rewriting the first `pick` to `verb`.

        The sequence editor is a Python snippet rather than `sed -i`, whose in-place flag differs
        between BSD and GNU. Stops at that step, which is how a user reaches an in-progress amend.
        """
        rewrite = (
            "import pathlib,sys;p=pathlib.Path(sys.argv[1]);"
            f"p.write_text(p.read_text().replace('pick', {verb!r}, 1))"
        )
        env = {
            **os.environ,
            "GIT_SEQUENCE_EDITOR": f"{shlex.quote(sys.executable)} -c {shlex.quote(rewrite)}",
            "GIT_EDITOR": "true",
        }
        _git("rebase", "-i", onto, cwd=worktree, check=False, env=env)

    def stop_rebase_clean(self, worktree: Path, onto: str, filename: str) -> None:
        """Leave `worktree` mid-rebase onto `onto` with a clean `git status`.

        Rebase until `filename` conflicts, then resolve it to the base's own version and stage
        it, so the index matches HEAD and `git status --porcelain` is empty while `rebase-merge/`
        survives with a correct `onto`. Interactive `break`/`edit` stops would be the other way
        to reach this state, but they need a sequence editor and set `amend`/non-`pick` markers.
        """
        from git_tree._git import _active_rebase_onto, _has_active_rebase

        self.git("-c", "core.editor=true", "rebase", onto, cwd=worktree, check=False)
        self.git("checkout", "--ours", "--", filename, cwd=worktree)
        self.git("add", filename, cwd=worktree)
        # Assert both halves of the promise, not just the clean one. Callers gate
        # destructive-command tests on this state, so a rebase that finished, or stopped onto
        # something else, would quietly turn those into tests of nothing.
        assert _has_active_rebase(worktree), "expected a stopped rebase, not a finished one"
        assert _active_rebase_onto(worktree) == self.git("rev-parse", onto), (
            f"expected the rebase to be stopped onto {onto}"
        )
        assert not self.git("status", "--porcelain", cwd=worktree), "expected a clean stop"


def stopped_rebase(repo: RepoHelper, tmp_path: Path, branch: str = "A") -> Path:
    """Fork `branch` off main, give each side a conflicting edit to shared.txt, and leave
    `branch`'s worktree mid-rebase onto main with a clean status. Returns that worktree."""
    repo.commit("shared.txt", "base", "base shared")
    repo.branch(branch, parent="main")
    wt = repo.worktree(branch, str(tmp_path / f"wt-{branch}"))
    (wt / "shared.txt").write_text(f"{branch} version")
    repo.git("add", "shared.txt", cwd=wt)
    repo.git("commit", "-m", f"{branch} edits shared", cwd=wt)
    repo.checkout("main")
    repo.commit("shared.txt", "main version", "main edits shared")
    repo.stop_rebase_clean(wt, "main", "shared.txt")
    return wt


def add_submodule(repo: RepoHelper, name: str, tmp_path: Path) -> Path:
    """Create a sub-repo and add it to `repo` as a submodule. Returns its path in the worktree."""
    sub_repo = tmp_path / f"sub-{name}"
    sub_repo.mkdir()
    _git("init", cwd=sub_repo)
    _git("config", "user.email", "test@test.com", cwd=sub_repo)
    _git("config", "user.name", "Test", cwd=sub_repo)
    (sub_repo / "readme.txt").write_text("sub content")
    _git("add", "readme.txt", cwd=sub_repo)
    _git("commit", "-m", "sub init", cwd=sub_repo)
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", str(sub_repo), name)
    repo.git("commit", "-m", f"add submodule {name}")
    return repo.work / name


def corrupt_submodule(worktree: Path, submodule_path: str) -> None:
    """Point a submodule's `.git` pointer at nothing, so its health check fails."""
    dot_git = worktree / submodule_path / ".git"
    dot_git.write_text("gitdir: /nonexistent/path/that/does/not/exist\n")


def allow_file_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let submodule clones over file:// through, for the code under test.

    `-c protocol.file.allow=always` covers only the test's own git calls; production code spawns
    its own git, so the setting has to reach those through the environment.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


@pytest.fixture(scope="session")
def _git_global_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A controlled global git config so tests never inherit the developer's ~/.gitconfig,
    keeping results deterministic across machines. rerere is off here; a test that needs to
    record a rerere resolution with its own git commands turns it on per-repo via
    RepoHelper.enable_rerere (repo config overrides this file)."""
    cfg = tmp_path_factory.mktemp("gitconfig") / "config"
    cfg.write_text(
        "[init]\n\tdefaultBranch = main\n"
        "[user]\n\tname = Test\n\temail = test@test.com\n"
        "[rerere]\n\tenabled = false\n"
        "[commit]\n\tgpgsign = false\n"
        "[tag]\n\tgpgsign = false\n"
        "[core]\n\tautocrlf = false\n\teditor = true\n"
    )
    return cfg


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch, _git_global_config: Path) -> None:
    """Route every test's git through the controlled global config and no system config, so
    results don't depend on the machine's git settings."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_git_global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory: pytest.TempPathFactory, _git_global_config: Path) -> Path:
    """Build the bare-origin + clone + initial-pushed-commit skeleton ONCE per session. Each
    `repo` copies this instead of re-running init/clone/commit/push (~7x cheaper per test).
    Built with the isolated global config baked into the environment so the session fixture
    doesn't depend on the function-scoped monkeypatch that `repo` uses."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(_git_global_config),
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    root = tmp_path_factory.mktemp("repo-template")

    origin = root / "origin.git"
    origin.mkdir()
    _git("init", "--bare", cwd=origin, env=env)

    work = root / "work"
    _git("clone", str(origin), str(work), cwd=root, env=env)
    _git("config", "user.email", "test@test.com", cwd=work, env=env)
    _git("config", "user.name", "Test", cwd=work, env=env)
    (work / "init.txt").write_text("init")
    _git("add", "init.txt", cwd=work, env=env)
    _git("commit", "-m", "initial commit", cwd=work, env=env)
    _git("push", "-u", "origin", "main", cwd=work, env=env)

    return root


@pytest.fixture
def repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_git_config: None,
    _repo_template: Path,
) -> RepoHelper:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    shutil.copytree(_repo_template / "origin.git", origin)
    shutil.copytree(_repo_template / "work", work)
    # The cloned remote URL still points at the template's origin; repoint it at this copy.
    _git("remote", "set-url", "origin", str(origin), cwd=work)

    helper = RepoHelper(work=work, origin=origin)
    monkeypatch.chdir(work)
    return helper


@pytest.fixture
def no_fzf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the fzf picker is consulted at all."""

    def _refuse(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("fzf picker should not be consulted")

    monkeypatch.setattr(FZF_SELECT, _refuse)


@pytest.fixture
def pick_fzf(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Stub the fzf picker to return `chosen`; call with no arguments for a cancelled pick."""

    def _pick(*chosen: str) -> None:
        monkeypatch.setattr(FZF_SELECT, lambda items, **kw: list(chosen))

    return _pick
