from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


def _git(*args: str, cwd: Path, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
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


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RepoHelper:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", cwd=origin)

    work = tmp_path / "work"
    _git("clone", str(origin), str(work), cwd=tmp_path)
    _git("config", "user.email", "test@test.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)

    helper = RepoHelper(work=work, origin=origin)
    helper.commit("init.txt", "init", "initial commit")
    helper.push("main")

    monkeypatch.chdir(work)
    return helper
