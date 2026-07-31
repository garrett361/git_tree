from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


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

    def stop_rebase_clean(self, worktree: Path, onto: str, filename: str) -> None:
        """Leave `worktree` mid-rebase onto `onto` with a clean `git status`.

        Rebase until `filename` conflicts, then resolve it to the base's own version and stage
        it, so the index matches HEAD and `git status --porcelain` is empty while `rebase-merge/`
        survives with a correct `onto`. Interactive `break`/`edit` stops would be the other way
        to reach this state, but they need a sequence editor and set `amend`/non-`pick` markers.
        """
        self.git("-c", "core.editor=true", "rebase", onto, cwd=worktree, check=False)
        self.git("checkout", "--ours", "--", filename, cwd=worktree)
        self.git("add", filename, cwd=worktree)
        assert not self.git("status", "--porcelain", cwd=worktree), "expected a clean stop"


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
