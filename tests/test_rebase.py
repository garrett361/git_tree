from __future__ import annotations

import pytest

from git_tree._errors import TreeError
from git_tree.cli import (
    _has_active_rebase,
    _root_remote,
    cmd_propagate,
    cmd_push,
    cmd_rebase,
    discover,
)

from .conftest import RepoHelper, cli_args


def _ns(target: str, yes: bool = False, branch: str | None = None) -> object:
    return cli_args(
        command="rebase", target=target, branch=branch, dry_run=False, no_auto_rerere=False, yes=yes
    )


def _no_confirm(_message: str) -> bool:
    raise AssertionError("confirm should not be consulted with --yes")


class TestRebase:
    def test_yes_skips_confirmation(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "advance main")
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)
        monkeypatch.chdir(wt)

        monkeypatch.setattr("builtins.input", _no_confirm)
        cmd_rebase(_ns(target="main", yes=True))

        log = repo.git("log", "--oneline", "feature")
        assert "advance main" in log
        assert "feature commit" in log

    def test_cascades_to_descendants(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "b commit", cwd=wt_b)
        repo.branch("c", parent="b")
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        (wt_c / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt_c)
        repo.git("commit", "-m", "c commit", cwd=wt_c)

        repo.checkout("main")
        repo.commit("m2.txt", "m2", "new main commit")

        monkeypatch.chdir(wt_b)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        c_log = repo.git("log", "--oneline", "c")
        assert "new main commit" in c_log
        assert "c commit" in c_log

    def test_excludes_old_parent_commits(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        """After squash-merge, rebase replays only child's unique commits, not parent's."""
        repo.branch("parent-branch", parent="main")
        wt_p = repo.worktree("parent-branch", str(tmp_path / "wt-parent"))
        (wt_p / "p1.txt").write_text("p1")
        repo.git("add", "p1.txt", cwd=wt_p)
        repo.git("commit", "-m", "parent commit 1", cwd=wt_p)
        (wt_p / "p2.txt").write_text("p2")
        repo.git("add", "p2.txt", cwd=wt_p)
        repo.git("commit", "-m", "parent commit 2", cwd=wt_p)

        repo.branch("child-branch", parent="parent-branch")
        wt_c = repo.worktree("child-branch", str(tmp_path / "wt-child"))
        (wt_c / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt_c)
        repo.git("commit", "-m", "child unique commit", cwd=wt_c)

        # Simulate squash-merge of parent-branch into main
        repo.checkout("main")
        repo.git("merge", "--squash", "parent-branch")
        repo.git("commit", "-m", "squash merge of parent-branch")

        monkeypatch.chdir(wt_c)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        log = repo.git("log", "--oneline", "child-branch")
        assert "child unique commit" in log
        assert "squash merge" in log
        assert "parent commit 1" not in log
        assert "parent commit 2" not in log

    def test_confirmation_decline_aborts(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)
        head_before = repo.git("rev-parse", "feature")

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        cmd_rebase(_ns(target="main"))

        assert repo.git("rev-parse", "feature") == head_before

    def test_dry_does_not_modify(self, repo: RepoHelper, capsys, tmp_path, monkeypatch) -> None:
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)
        head_before = repo.git("rev-parse", "feature")

        monkeypatch.chdir(wt)
        cmd_rebase(cli_args(command="rebase", target="main", dry_run=True, no_auto_rerere=False))

        assert repo.git("rev-parse", "feature") == head_before
        out = capsys.readouterr().out
        assert "Rebasing onto" in out

    def test_conflict_aborts(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        repo.commit("shared.txt", "original", "base")
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "shared.txt").write_text("feature version")
        repo.git("add", "shared.txt", cwd=wt)
        repo.git("commit", "-m", "feature modifies shared", cwd=wt)

        repo.checkout("main")
        repo.commit("shared.txt", "main version", "main modifies shared")

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")

        with pytest.raises(SystemExit):
            cmd_rebase(_ns(target="main"))

    def test_rebase_works_in_main_worktree(self, repo: RepoHelper, monkeypatch) -> None:
        """Branch checked out in main worktree (no secondary worktree) can be rebased."""
        repo.commit("a1.txt", "a1", "advance main")
        repo.branch("feature", parent="main")
        repo.checkout("feature")
        repo.commit("f1.txt", "f1", "on feature")

        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        log = repo.git("log", "--oneline", "feature")
        assert "on feature" in log

    def test_rebase_propagates_to_one_child_not_sibling(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """main -> dev1, dev2: rebasing dev1 onto an advanced main updates dev1 (and its
        descendants) but leaves the sibling dev2 untouched."""
        repo.branch("dev1", parent="main")
        wt1 = repo.worktree("dev1", str(tmp_path / "wt-dev1"))
        (wt1 / "d1.txt").write_text("d1")
        repo.git("add", "d1.txt", cwd=wt1)
        repo.git("commit", "-m", "dev1 commit", cwd=wt1)

        repo.branch("dev2", parent="main")
        wt2 = repo.worktree("dev2", str(tmp_path / "wt-dev2"))
        (wt2 / "d2.txt").write_text("d2")
        repo.git("add", "d2.txt", cwd=wt2)
        repo.git("commit", "-m", "dev2 commit", cwd=wt2)

        # Bring new upstream work into main.
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "new main commit")

        # Propagate main into dev1 only, via rebase from dev1's worktree.
        monkeypatch.chdir(wt1)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        dev1_log = repo.git("log", "--oneline", "dev1")
        assert "new main commit" in dev1_log
        assert "dev1 commit" in dev1_log

        dev2_log = repo.git("log", "--oneline", "dev2")
        assert "new main commit" not in dev2_log  # sibling not propagated
        assert "dev2 commit" in dev2_log

    def test_rebase_diverged_parent_preserves_commits(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """When the parent advanced without propagate, rebase preserves the child's commits."""
        repo.commit("a1.txt", "a1", "base for feature")
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "first on feature", cwd=wt)
        (wt / "f2.txt").write_text("f2")
        repo.git("add", "f2.txt", cwd=wt)
        repo.git("commit", "-m", "second on feature", cwd=wt)

        # Advance main (parent diverges from feature's fork point)
        repo.checkout("main")
        repo.commit("a2.txt", "a2", "advance main past fork")

        # Create a new target branch
        repo.git("branch", "new-base")

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="new-base"))

        log = repo.git("log", "--oneline", "feature")
        assert "first on feature" in log
        assert "second on feature" in log

    def test_invalid_targets_raise_before_side_effects(
        self, repo: RepoHelper, monkeypatch, capsys
    ) -> None:
        # main <- a <- b. Each invalid target (own descendant, nonexistent, self) must be
        # rejected before the confirm prompt and before any rebase: a's tip and tree-parent
        # stay put after each attempt.
        repo.branch("a", parent="main")
        repo.branch("b", parent="a")
        repo.checkout("a")
        tip_before = repo.git("rev-parse", "a")

        # input must never be consulted — the guard aborts before the confirm prompt.
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("reached confirm"))

        # Rebasing a onto its own descendant b would loop.
        with pytest.raises(TreeError):
            cmd_rebase(_ns(target="b"))
        assert repo.git("rev-parse", "a") == tip_before
        assert discover().parent_of["a"] == "main"

        # A typo'd / nonexistent target is rejected with a clear message.
        with pytest.raises(TreeError):
            cmd_rebase(_ns(target="no-such-branch"))
        assert "is not a local branch" in capsys.readouterr().err
        assert repo.git("rev-parse", "a") == tip_before
        assert discover().parent_of["a"] == "main"

        # Rebasing a onto itself is rejected.
        with pytest.raises(TreeError):
            cmd_rebase(_ns(target="a"))
        assert repo.git("rev-parse", "a") == tip_before
        assert discover().parent_of["a"] == "main"

        # A bare commit (or tag) is rejected: tree-parents must be local branches, else the
        # written edge would orphan `a` on the next discover().
        commit_sha = repo.git("rev-parse", "main")
        with pytest.raises(TreeError):
            cmd_rebase(_ns(target=commit_sha))
        assert "is not a local branch" in capsys.readouterr().err
        assert discover().parent_of["a"] == "main"

    def test_rebase_out_of_tree_carries_remote_and_push_resolves(
        self, repo: RepoHelper, monkeypatch, tmp_path, capsys
    ) -> None:
        # main(root, remote=origin) roots both `a` and `sibling`. Rebasing `a` onto a fresh
        # out-of-tree branch re-roots a's tree; the remote must FOLLOW so push still
        # resolves, while being COPIED not moved — main keeps its remote so `sibling`,
        # still rooted there, continues to resolve it.
        repo.git("config", "branch.main.remote", "origin")
        repo.branch("a", parent="main")
        wt = repo.worktree("a", str(tmp_path / "wt-a"))
        (wt / "a1.txt").write_text("a1")
        repo.git("add", "a1.txt", cwd=wt)
        repo.git("commit", "-m", "a commit", cwd=wt)
        repo.branch("sibling", parent="main")
        repo.git("branch", "new-base")  # not in the tree, no remote

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="new-base"))

        assert _root_remote(discover(), "a") == ("new-base", "origin")
        # The actual symptom of the bug: push could not resolve a remote. Now it can.
        capsys.readouterr()
        cmd_push(cli_args(command="push", dry_run=True))
        assert "Pushing to origin" in capsys.readouterr().out

        # main keeps its remote so the still-rooted sibling continues to resolve it.
        assert repo.git("config", "branch.main.remote") == "origin"
        assert _root_remote(discover(), "sibling") == ("main", "origin")

    def test_rebase_within_tree_leaves_remote_untouched(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        # main(root, remote=origin) <- base <- child. Rebasing child onto main stays in
        # the same tree (root unchanged), so no remote should be created or moved.
        repo.git("config", "branch.main.remote", "origin")
        repo.git("branch", "base")
        repo.set_parent("base", "main")
        repo.worktree("base", str(tmp_path / "wt-base"))
        repo.branch("child", parent="base")
        wt = repo.worktree("child", str(tmp_path / "wt-child"))
        (wt / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt)
        repo.git("commit", "-m", "child commit", cwd=wt)

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="main"))

        assert discover().parent_of["child"] == "main"
        assert repo.git("config", "branch.main.remote") == "origin"
        assert repo.git("config", "branch.child.remote", check=False) == ""
        assert repo.git("config", "branch.base.remote", check=False) == ""

    def test_rebase_onto_foreign_tree_keeps_its_remote(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        # feature is under main(remote=origin); `other` is a separate root with its own
        # remote. Rebasing feature onto other must NOT overwrite other's remote; feature
        # adopts other's tree remote via resolution.
        repo.git("config", "branch.main.remote", "origin")
        repo.git("branch", "other")
        repo.git("config", "branch.other.remote", "upstream")
        repo.branch("feature", parent="main")
        wt = repo.worktree("feature", str(tmp_path / "wt-feature"))
        (wt / "f1.txt").write_text("f1")
        repo.git("add", "f1.txt", cwd=wt)
        repo.git("commit", "-m", "feature commit", cwd=wt)

        monkeypatch.chdir(wt)
        monkeypatch.setattr("builtins.input", lambda _: "y")
        cmd_rebase(_ns(target="other"))

        assert repo.git("config", "branch.other.remote") == "upstream"
        assert _root_remote(discover(), "feature") == ("other", "upstream")


class TestNamedBranch:
    """`git tree rebase <target> [branch]` acts on the named branch, wherever it is run from."""

    def _stack(self, repo: RepoHelper, tmp_path):
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "b commit", cwd=wt_b)
        repo.branch("c", parent="b")
        wt_c = repo.worktree("c", str(tmp_path / "wt-c"))
        (wt_c / "c1.txt").write_text("c1")
        repo.git("add", "c1.txt", cwd=wt_c)
        repo.git("commit", "-m", "c commit", cwd=wt_c)
        return wt_b, wt_c

    def test_rebases_named_branch_and_cascades_without_chdir(
        self, repo: RepoHelper, tmp_path
    ) -> None:
        self._stack(repo, tmp_path)
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "new main commit")

        cmd_rebase(_ns(target="main", branch="b", yes=True))  # cwd is main's worktree

        c_log = repo.git("log", "--oneline", "c")
        assert "new main commit" in c_log
        assert "c commit" in c_log

    def test_named_branch_wins_over_current_branch(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        wt_b, wt_c = self._stack(repo, tmp_path)
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "new main commit")
        c_before = repo.git("rev-parse", "c")

        monkeypatch.chdir(wt_c)  # standing on c, but naming b
        cmd_rebase(_ns(target="main", branch="b", yes=True))

        assert discover().parent_of["b"] == "main"
        assert discover().parent_of["c"] == "b"  # c's edge untouched
        assert repo.git("rev-parse", "c") != c_before  # c only moved by the cascade

    def test_works_from_a_detached_head(self, repo: RepoHelper, monkeypatch, tmp_path) -> None:
        self._stack(repo, tmp_path)
        repo.checkout("main")
        repo.commit("m2.txt", "m2", "new main commit")
        repo.git("checkout", "--detach")

        cmd_rebase(_ns(target="main", branch="b", yes=True))

        assert "new main commit" in repo.git("log", "--oneline", "b")

    def test_mid_rebase_branch_is_refused_without_touching_it(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """Naming a stuck branch must point at the resume verb, not drive its rebase to the end.

        The conflict is resolved and staged first, which is the state `_require_clean_state`
        admits as a resume point. Without the guard, `_skip_empty_commits` `git rebase --skip`s
        past B's own commit and reports success, losing it permanently.
        """
        repo.commit("shared.txt", "original", "base shared")
        repo.git("branch", "T", "main")
        repo.set_parent("T", "main")
        wt_T = repo.worktree("T", str(tmp_path / "wt-T"))
        (wt_T / "shared.txt").write_text("T version")
        repo.git("add", "shared.txt", cwd=wt_T)
        repo.git("commit", "-m", "T edits shared", cwd=wt_T)

        repo.git("branch", "B", "main")
        repo.set_parent("B", "main")
        wt_B = repo.worktree("B", str(tmp_path / "wt-B"))
        (wt_B / "shared.txt").write_text("B version")
        repo.git("add", "shared.txt", cwd=wt_B)
        repo.git("commit", "-m", "B edits shared", cwd=wt_B)

        monkeypatch.chdir(wt_B)
        with pytest.raises(SystemExit):
            cmd_rebase(_ns(target="T", yes=True))
        assert _has_active_rebase(wt_B)

        (wt_B / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_B)

        monkeypatch.chdir(repo.work)
        with pytest.raises(SystemExit) as exc:
            cmd_rebase(_ns(target="main", branch="B", yes=True))

        assert exc.value.code == 4
        assert "git tree propagate B" in exc.value.message
        assert _has_active_rebase(wt_B)  # left alone
        assert discover().parent_of["B"] == "T"  # not reparented onto main
        assert "B edits shared" in repo.git("log", "--oneline", "B")  # commit not skipped away

    def test_foreign_mid_rebase_is_not_sent_to_propagate(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        """`propagate` refuses a rebase git-tree did not start, so advising it would dead-end."""
        repo.commit("shared.txt", "original", "base shared")
        repo.git("branch", "U", "main")
        repo.checkout("U")
        repo.commit("shared.txt", "U version", "U edits shared")  # diverges, so B will conflict
        repo.checkout("main")

        repo.branch("B", parent="main")
        wt_B = repo.worktree("B", str(tmp_path / "wt-B"))
        (wt_B / "shared.txt").write_text("B version")
        repo.git("add", "shared.txt", cwd=wt_B)
        repo.git("commit", "-m", "B edits shared", cwd=wt_B)
        repo.git("rebase", "U", cwd=wt_B, check=False)  # hand-started, conflicts
        assert _has_active_rebase(wt_B)

        with pytest.raises(SystemExit) as exc:
            cmd_rebase(_ns(target="main", branch="B", yes=True))

        assert exc.value.code == 4
        assert "git rebase --abort" in exc.value.message
        assert "propagate" not in exc.value.message

    def test_nonexistent_branch_is_named_as_such(self, repo: RepoHelper) -> None:
        with pytest.raises(SystemExit) as exc:
            cmd_rebase(_ns(target="main", branch="nope", yes=True))
        assert exc.value.code == 4
        assert "No such branch" in exc.value.message

    def test_branch_outside_the_tree_exits_5(self, repo: RepoHelper) -> None:
        repo.git("branch", "loose", "main")  # a real branch, but no tree-parent
        with pytest.raises(SystemExit) as exc:
            cmd_rebase(_ns(target="main", branch="loose", yes=True))
        assert exc.value.code == 5

    def test_mid_rebase_descendant_is_refused_before_anything_moves(
        self, repo: RepoHelper, tmp_path
    ) -> None:
        """Rebasing a branch necessarily invalidates any rebase in progress below it.

        `_require_clean_state` admits a resolved git-tree mid-rebase as a resume point, which is
        right for `propagate` and wrong here: by the time the cascade reaches C, B has been
        reparented and rewritten, so C's `onto` is no longer an ancestor of B and the run
        dead-ends on "git-tree did not start it". That state cannot be reached again through
        git-tree, and the advised `git rebase --abort` throws away C's resolution.
        """
        repo.commit("shared.txt", "base", "base shared")
        repo.git("branch", "T", "main")
        repo.set_parent("T", "main")
        wt_T = repo.worktree("T", str(tmp_path / "wt-T"))
        (wt_T / "t.txt").write_text("t")  # T must be ahead, or rebasing B onto it moves nothing
        repo.git("add", "t.txt", cwd=wt_T)
        repo.git("commit", "-m", "T adds t", cwd=wt_T)

        repo.git("branch", "B", "main")
        repo.set_parent("B", "main")
        wt_B = repo.worktree("B", str(tmp_path / "wt-B"))
        repo.git("branch", "C", "B")
        repo.set_parent("C", "B")
        wt_C = repo.worktree("C", str(tmp_path / "wt-C"))
        (wt_C / "shared.txt").write_text("C version")
        repo.git("add", "shared.txt", cwd=wt_C)
        repo.git("commit", "-m", "C edits shared", cwd=wt_C)
        # B advances after C forked, so C conflicts when replayed onto it.
        (wt_B / "shared.txt").write_text("B version")
        repo.git("add", "shared.txt", cwd=wt_B)
        repo.git("commit", "-m", "B edits shared", cwd=wt_B)
        repo.stop_rebase_clean(wt_C, "B", "shared.txt")
        b_before = repo.git("rev-parse", "B")

        with pytest.raises(TreeError) as exc:
            cmd_rebase(_ns(target="T", branch="B", yes=True))

        assert exc.value.code == 4
        assert "C" in exc.value.message
        assert discover().parent_of["B"] == "main"  # refused before the edge was rewritten
        assert repo.git("rev-parse", "B") == b_before
        assert _has_active_rebase(wt_C)


class TestRebaseStashAdvice:
    def test_pop_conflict_advice_names_the_stash_commit(
        self, repo: RepoHelper, tmp_path, capsys
    ) -> None:
        """The rest of the tool moved to `git stash apply <sha>`; this site did not.

        Running the advised `git stash pop` after the pop already failed errors with `needs
        merge`, and `stash@{0}` can point at another worktree's entry by the time it is read,
        since `refs/stash` is repo-wide.
        """
        repo.commit("x.txt", "orig", "base with x")
        repo.git("branch", "T", "main")
        repo.set_parent("T", "main")
        wt_T = repo.worktree("T", str(tmp_path / "wt-T"))
        (wt_T / "x.txt").write_text("T version")
        repo.git("add", "x.txt", cwd=wt_T)
        repo.git("commit", "-m", "T changes x", cwd=wt_T)

        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "b1.txt").write_text("b1")
        repo.git("add", "b1.txt", cwd=wt_b)
        repo.git("commit", "-m", "b commit", cwd=wt_b)
        (wt_b / "x.txt").write_text("b dirty")  # uncommitted, collides on pop

        cmd_rebase(_ns(target="T", branch="b", yes=True))

        err = capsys.readouterr().err
        assert "git stash pop" not in err
        assert f"git stash apply {repo.git('rev-parse', 'refs/stash')}" in err


class TestRebaseResumeViaPropagate:
    """A `git tree rebase` conflict is resumed with `git tree propagate <branch>` (the reparent
    is already committed, so propagate finishes the branch onto its new target and cascades)."""

    def test_phase1_conflict_resumed_and_cousin_untouched(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        # `rebase B onto T` conflicts on B itself; resume with `propagate B`. Must finish B onto
        # T and leave T's drifted cousin C untouched (tight scope).
        repo.commit("shared.txt", "original", "base shared")
        repo.git("branch", "R", "main")
        repo.git("branch", "T", "R")
        repo.set_parent("T", "R")
        wt_T = repo.worktree("T", str(tmp_path / "wt-T"))

        repo.git("branch", "C", "T")  # cousin, forked before T advances
        repo.set_parent("C", "T")
        wt_C = repo.worktree("C", str(tmp_path / "wt-C"))
        (wt_C / "c-only.txt").write_text("c")
        repo.git("add", "c-only.txt", cwd=wt_C)
        repo.git("commit", "-m", "C adds c-only", cwd=wt_C)

        (wt_T / "shared.txt").write_text("T version")  # advance T -> C drifts
        repo.git("add", "shared.txt", cwd=wt_T)
        repo.git("commit", "-m", "T edits shared", cwd=wt_T)

        repo.git("branch", "B", "R")
        repo.set_parent("B", "R")
        wt_B = repo.worktree("B", str(tmp_path / "wt-B"))
        (wt_B / "shared.txt").write_text("B version")
        repo.git("add", "shared.txt", cwd=wt_B)
        repo.git("commit", "-m", "B edits shared", cwd=wt_B)

        c_before = repo.git("rev-parse", "C")

        monkeypatch.chdir(wt_B)
        with pytest.raises(SystemExit) as exc:
            cmd_rebase(_ns(target="T", yes=True))
        assert _has_active_rebase(wt_B)
        assert exc.value.remedy == ["git", "tree", "propagate", "B"]

        (wt_B / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_B)
        cmd_propagate(cli_args(branch="B", dry_run=False, no_auto_rerere=False, yes=False))

        assert not _has_active_rebase(wt_B)
        assert discover().parent_of["B"] == "T"  # reparent stuck
        assert "T edits shared" in repo.git("log", "--oneline", "B")  # B finished onto T
        assert repo.git("rev-parse", "C") == c_before  # cousin untouched

    def test_phase2_descendant_conflict_resumed_via_propagate(
        self, repo: RepoHelper, monkeypatch, tmp_path
    ) -> None:
        # `rebase B onto T` succeeds on B, then a descendant D conflicts. Resume with
        # `propagate B` (message names it); D is finished.
        repo.commit("shared.txt", "original", "base")
        repo.git("branch", "T", "main")
        repo.set_parent("T", "main")
        wt_T = repo.worktree("T", str(tmp_path / "wt-T"))
        (wt_T / "shared.txt").write_text("T version")
        repo.git("add", "shared.txt", cwd=wt_T)
        repo.git("commit", "-m", "T edits shared", cwd=wt_T)

        repo.git("branch", "B", "main")
        repo.set_parent("B", "main")
        wt_B = repo.worktree("B", str(tmp_path / "wt-B"))
        (wt_B / "b.txt").write_text("b")  # disjoint from shared -> B rebases onto T cleanly
        repo.git("add", "b.txt", cwd=wt_B)
        repo.git("commit", "-m", "B adds b", cwd=wt_B)

        repo.git("branch", "D", "B")
        repo.set_parent("D", "B")
        wt_D = repo.worktree("D", str(tmp_path / "wt-D"))
        (wt_D / "shared.txt").write_text("D version")  # conflicts with T's shared once B is on T
        repo.git("add", "shared.txt", cwd=wt_D)
        repo.git("commit", "-m", "D edits shared", cwd=wt_D)

        monkeypatch.chdir(wt_B)
        with pytest.raises(SystemExit) as exc:
            cmd_rebase(_ns(target="T", yes=True))
        assert not _has_active_rebase(wt_B)  # B finished
        assert _has_active_rebase(wt_D)  # D is the stuck one
        assert exc.value.remedy == ["git", "tree", "propagate", "B"]

        (wt_D / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_D)
        cmd_propagate(cli_args(branch="B", dry_run=False, no_auto_rerere=False, yes=False))
        assert not _has_active_rebase(wt_D)
