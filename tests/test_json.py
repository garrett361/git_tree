from __future__ import annotations

import json

import pytest

from git_tree.cli import _has_active_rebase, main

from .conftest import RepoHelper


class TestSuccessEnvelope:
    def test_minimal_success(self, repo: RepoHelper, capsys) -> None:
        # No-op propagate on a rootless main: a bare success envelope, no `data`.
        main(["propagate", "--json", "-y"])
        obj = json.loads(capsys.readouterr().out)
        assert obj["command"] == "propagate"
        assert obj["ok"] is True
        assert "error" not in obj

    def test_forest_envelope_is_backward_compatible(self, repo: RepoHelper, capsys) -> None:
        repo.branch("feat", parent="main")
        main(["--json"])
        obj = json.loads(capsys.readouterr().out)
        assert obj["command"] == "tree" and obj["ok"] is True
        # The existing forest keys remain present as envelope siblings.
        assert obj["roots"] == ["main"]
        assert {b["name"] for b in obj["branches"]} >= {"main", "feat"}

    def test_stdout_is_exactly_one_json_object(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.branch("feat", parent="main")
        repo.worktree("feat", str(tmp_path / "wt-feat"))
        repo.commit("m2.txt", "m2", "advance")
        main(["propagate", "--json", "-y"])
        out = capsys.readouterr().out
        json.loads(out)  # parses as a single object
        assert "+ git" not in out  # git_echo diagnostics went to stderr, not stdout


class TestErrorEnvelope:
    def test_not_a_tree_branch(self, repo: RepoHelper, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["push", "--json"])
        assert exc.value.code == 5
        err = json.loads(capsys.readouterr().out)["error"]
        assert err["kind"] == "not_a_tree_branch"
        assert err["code"] == 5
        assert err["message"]

    def test_log_json_is_usage_error(self, repo: RepoHelper, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["log", "--json"])
        assert exc.value.code == 2
        assert json.loads(capsys.readouterr().out)["error"]["kind"] == "usage"

    def test_json_implies_non_interactive(self, repo: RepoHelper, capsys) -> None:
        # `attach` with no parent would prompt; --json alone must error, no --no-input needed.
        repo.git("branch", "solo")
        repo.checkout("solo")
        with pytest.raises(SystemExit) as exc:
            main(["attach", "--json"])
        assert exc.value.code == 4
        obj = json.loads(capsys.readouterr().out)
        assert obj["ok"] is False and obj["error"]["kind"] == "input_required"

    def test_confirmation_required(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.branch("feat", parent="main")
        repo.worktree("feat", str(tmp_path / "wt-feat"))
        with pytest.raises(SystemExit) as exc:
            main(["remove", "feat", "--json"])
        assert exc.value.code == 4
        err = json.loads(capsys.readouterr().out)["error"]
        assert err["kind"] == "confirmation_required"
        assert "remedy" not in err  # the agent already knows its own command
        assert "-y" in err["message"]  # the message names the flag to add

    def test_conflict_envelope(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.commit("shared.txt", "base", "base")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b change", cwd=wt_b)
        repo.checkout("main")
        repo.commit("shared.txt", "from main", "conflicting change")
        with pytest.raises(SystemExit) as exc:
            main(["propagate", "main", "--json", "-y"])
        assert exc.value.code == 3
        err = json.loads(capsys.readouterr().out)["error"]
        assert err["kind"] == "conflict"
        assert err["branch"] == "b"
        assert err["conflicted_files"] == ["shared.txt"]
        assert err["worktree"]
        assert err["remedy"] == ["git", "tree", "continue"]


class TestContinue:
    def _stop_on_conflict(self, repo: RepoHelper, capsys, tmp_path):
        repo.commit("shared.txt", "base", "base")
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b change", cwd=wt_b)
        repo.checkout("main")
        repo.commit("shared.txt", "from main", "conflicting change")
        with pytest.raises(SystemExit) as exc:
            main(["propagate", "main", "--no-input", "-y"])
        assert exc.value.code == 3
        capsys.readouterr()  # drain setup output so the next envelope stands alone
        return wt_b

    def test_resumes_after_resolution(self, repo: RepoHelper, capsys, tmp_path) -> None:
        wt_b = self._stop_on_conflict(repo, capsys, tmp_path)
        (wt_b / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_b)
        main(["continue", "--json"])
        obj = json.loads(capsys.readouterr().out)
        assert obj["command"] == "continue" and obj["ok"] is True
        assert "b change" in repo.git("log", "--oneline", "b")  # b is rebased onto main
        assert not _has_active_rebase(wt_b)  # rebase finished

    def test_resumes_whole_cascade_not_just_stuck_subtree(
        self, repo: RepoHelper, capsys, tmp_path
    ) -> None:
        # main -> {aconf, zclean}. `aconf` (alphabetically first, so processed first) conflicts;
        # `zclean` is a clean sibling queued *after* it and thus skipped by the aborted cascade.
        # continue must resume the whole tree, not just aconf's (empty) subtree, or zclean stays
        # stale.
        repo.commit("shared.txt", "base", "base")
        repo.branch("aconf", parent="main")
        wt_a = repo.worktree("aconf", str(tmp_path / "wt-a"))
        (wt_a / "shared.txt").write_text("from aconf")
        repo.git("add", "shared.txt", cwd=wt_a)
        repo.git("commit", "-m", "aconf change", cwd=wt_a)
        repo.branch("zclean", parent="main")
        wt_z = repo.worktree("zclean", str(tmp_path / "wt-z"))
        (wt_z / "z.txt").write_text("z")
        repo.git("add", "z.txt", cwd=wt_z)
        repo.git("commit", "-m", "zclean change", cwd=wt_z)
        repo.checkout("main")
        repo.commit("shared.txt", "from main", "advance main (conflicts with aconf)")

        with pytest.raises(SystemExit) as exc:
            main(["propagate", "main", "--no-input", "-y"])
        assert exc.value.code == 3  # aconf conflicts, zclean skipped
        capsys.readouterr()

        (wt_a / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_a)
        main(["continue", "--json"])
        assert json.loads(capsys.readouterr().out)["ok"] is True

        main(["--json"])
        by = {b["name"]: b for b in json.loads(capsys.readouterr().out)["branches"]}
        assert by["aconf"]["pending_from_parent"] == 0  # resolved branch caught up
        assert by["zclean"]["pending_from_parent"] == 0  # skipped sibling also rebased

    def test_resume_errors_cleanly_on_worktreeless_branch(
        self, repo: RepoHelper, capsys, tmp_path
    ) -> None:
        # The whole-tree resume can reach a worktree-less branch outside the original scope; it
        # must produce a clean precondition error, not a raw AssertionError deep in _rebase_branch.
        repo.commit("shared.txt", "base", "base")
        repo.branch("sub", parent="main")
        wt_sub = repo.worktree("sub", str(tmp_path / "wt-sub"))
        repo.branch("achild", parent="sub")
        wt_c = repo.worktree("achild", str(tmp_path / "wt-c"))
        (wt_c / "shared.txt").write_text("from child")
        repo.git("add", "shared.txt", cwd=wt_c)
        repo.git("commit", "-m", "child change", cwd=wt_c)
        (wt_sub / "shared.txt").write_text("from sub")
        repo.git("add", "shared.txt", cwd=wt_sub)
        repo.git("commit", "-m", "sub change", cwd=wt_sub)
        repo.branch("nowt", parent="main")  # worktree-less sibling, outside `propagate sub` scope

        with pytest.raises(SystemExit) as exc:
            main(["propagate", "sub", "--no-input", "-y"])
        assert exc.value.code == 3  # achild conflicts
        capsys.readouterr()

        (wt_c / "shared.txt").write_text("resolved")
        repo.git("add", "shared.txt", cwd=wt_c)
        with pytest.raises(SystemExit) as exc:
            main(["continue", "--json"])
        assert exc.value.code == 4  # nowt has no worktree -> clean precondition, not assert
        err = json.loads(capsys.readouterr().out)["error"]
        assert "nowt" in err["branches"]

    def test_unresolved_conflicts_errors(self, repo: RepoHelper, capsys, tmp_path) -> None:
        self._stop_on_conflict(repo, capsys, tmp_path)  # leave it unresolved
        with pytest.raises(SystemExit) as exc:
            main(["continue", "--json"])
        assert exc.value.code == 4
        assert json.loads(capsys.readouterr().out)["error"]["kind"] == "unresolved_conflicts"

    def test_no_rebase_in_progress_errors(self, repo: RepoHelper, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["continue", "--json"])
        assert exc.value.code == 4
        assert json.loads(capsys.readouterr().out)["ok"] is False


class TestPushJson:
    def _child_with_commit(self, repo: RepoHelper, tmp_path):
        repo.branch("child", parent="main")
        wt_c = repo.worktree("child", str(tmp_path / "wt-c"))
        (wt_c / "c.txt").write_text("c")
        repo.git("add", "c.txt", cwd=wt_c)
        repo.git("commit", "-m", "child commit", cwd=wt_c)

    def test_skipped_stale_branch_surfaced(self, repo: RepoHelper, capsys, tmp_path) -> None:
        # Advance main past child's fork so child is stale; pushing from main pushes main but
        # skips child. A bare {ok:true} would hide that — the agent must see it in `skipped`.
        self._child_with_commit(repo, tmp_path)
        repo.commit("m2.txt", "m2", "advance main past child's fork")
        main(["push", "--json", "-y"])
        obj = json.loads(capsys.readouterr().out)
        assert obj["ok"] is True
        assert {"branch": "child", "reason": "stale"} in obj["skipped"]

    def test_clean_push_reports_empty_skipped(self, repo: RepoHelper, capsys, tmp_path) -> None:
        self._child_with_commit(repo, tmp_path)
        main(["push", "--json", "-y"])
        obj = json.loads(capsys.readouterr().out)
        assert obj["ok"] is True
        assert obj["skipped"] == []

    def test_blocked_descendant_reports_ancestor_not_pushed(
        self, repo: RepoHelper, capsys, tmp_path
    ) -> None:
        # main -> aaa -> bbb; advance main so aaa is stale. aaa is skipped (stale); bbb, though
        # not itself stale, is blocked because its ancestor aaa wasn't pushed.
        repo.branch("aaa", parent="main")
        wt_a = repo.worktree("aaa", str(tmp_path / "wt-a"))
        (wt_a / "a.txt").write_text("a")
        repo.git("add", "a.txt", cwd=wt_a)
        repo.git("commit", "-m", "a commit", cwd=wt_a)
        repo.git("branch", "bbb", "aaa")  # start bbb at aaa's tip -> a true descendant
        repo.set_parent("bbb", "aaa")
        wt_b = repo.worktree("bbb", str(tmp_path / "wt-b"))
        (wt_b / "b.txt").write_text("b")
        repo.git("add", "b.txt", cwd=wt_b)
        repo.git("commit", "-m", "b commit", cwd=wt_b)
        repo.commit("m2.txt", "m2", "advance main past aaa's fork")  # aaa now stale
        main(["push", "--json", "-y"])
        skipped = json.loads(capsys.readouterr().out)["skipped"]
        assert {"branch": "aaa", "reason": "stale"} in skipped
        assert {"branch": "bbb", "reason": "ancestor_not_pushed"} in skipped
