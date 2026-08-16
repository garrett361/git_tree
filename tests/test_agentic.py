from __future__ import annotations

import argparse
import json
import os
import re

import pytest

from git_tree._cmd_propagate import cmd_propagate
from git_tree._render import _render_completions
from git_tree.cli import _build_parser, main

from .conftest import RepoHelper, cli_args


def _prop_ns(**kw) -> argparse.Namespace:
    return cli_args(
        dry_run=kw.get("dry_run", False),
        no_auto_rerere=kw.get("no_auto_rerere", False),
        branch=kw.get("branch"),
        yes=kw.get("yes", False),
        no_input=kw.get("no_input", False),
    )


class TestJson:
    def test_full_forest_with_root_status(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.branch("feat", parent="main")
        wt_feat = repo.worktree("feat", str(tmp_path / "wt-feat"))
        (wt_feat / "f.txt").write_text("f")
        repo.git("add", "f.txt", cwd=wt_feat)
        repo.git("commit", "-m", "on feat", cwd=wt_feat)
        repo.branch("feat2", parent="feat")  # no worktree

        # Make the root (main, in the primary worktree) both ahead of origin and dirty.
        repo.commit("m2.txt", "m2", "ahead of origin")
        (repo.work / "dirty.txt").write_text("dirty")  # untracked -> dirty

        main(["--json"])
        data = json.loads(capsys.readouterr().out)

        assert data["roots"] == ["main"]
        by = {b["name"]: b for b in data["branches"]}
        assert set(by) == {"main", "feat", "feat2"}

        # Root status must be populated, not null (the root-status regression guard).
        root = by["main"]
        assert root["parent"] is None
        assert root["pending_from_parent"] is None
        assert root["worktree"] is not None
        assert root["dirty"] is True
        assert root["untracked"] == 1
        assert root["ahead"] == 1
        assert root["behind"] == 0

        assert by["feat"]["parent"] == "main"
        assert by["feat"]["children"] == ["feat2"]
        assert by["feat"]["root"] == "main"
        assert by["feat"]["fork_commit"]
        assert by["feat"]["worktree"] is not None
        assert by["feat"]["pending_from_parent"] == 1  # m2 landed on main after feat forked

        # A branch with no worktree has null status.
        assert by["feat2"]["parent"] == "feat"
        assert by["feat2"]["worktree"] is None
        assert by["feat2"]["dirty"] is None
        assert by["feat2"]["ahead"] is None

    def test_topological_order_roots_first(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.branch("feat", parent="main")
        repo.branch("feat2", parent="feat")
        main(["--json"])
        names = [b["name"] for b in json.loads(capsys.readouterr().out)["branches"]]
        assert names.index("main") < names.index("feat") < names.index("feat2")

    def test_cycle_surfaced_and_stdout_is_valid_json(
        self, repo: RepoHelper, capsys, tmp_path
    ) -> None:
        repo.branch("feat", parent="main")
        repo.git("branch", "x")
        repo.git("branch", "y")
        repo.set_parent("x", "y")
        repo.set_parent("y", "x")  # unrelated x <-> y cycle
        repo.worktree("x", str(tmp_path / "wt-x"))  # a cyclic node with a worktree

        main(["--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)  # valid JSON despite the stderr warning
        assert "cycle" in captured.err
        assert any(set(c) == {"x", "y"} for c in data["cycles"])
        # Healthy tree still present; the cyclic branches now also appear, tagged, with status.
        by = {b["name"]: b for b in data["branches"]}
        assert {"main", "feat", "x", "y"} <= set(by)
        assert by["x"]["cyclic"] is True and by["y"]["cyclic"] is True
        assert by["x"]["worktree"] is not None and by["x"]["dirty"] is False

    def test_orphan_surfaced(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.git("branch", "ghost")
        repo.git("config", "branch.ghost.tree-parent-branch", "gone")  # parent doesn't exist
        repo.worktree("ghost", str(tmp_path / "wt-ghost"))  # orphan with a worktree

        main(["--json"])
        data = json.loads(capsys.readouterr().out)
        assert ["ghost", "gone"] in data["orphans"]
        # The orphan is now also in branches[] with its worktree + a marker, so an agent
        # repairing the tree can see its state rather than shelling out to raw git.
        ghost = next(b for b in data["branches"] if b["name"] == "ghost")
        assert ghost["orphaned_parent"] == "gone"
        assert ghost["worktree"] is not None
        assert ghost["dirty"] is False

    def test_malformed_unrelated_parent_does_not_crash(self, repo: RepoHelper, capsys) -> None:
        # A branch whose configured parent shares no history has no merge-base. --json must
        # degrade gracefully (pending_from_parent 0), not crash with empty stdout.
        repo.git("checkout", "--orphan", "lonely")
        (repo.work / "x.txt").write_text("x")
        repo.git("add", "x.txt")
        repo.git("commit", "-m", "orphan root")
        repo.git("checkout", "main")
        repo.git("config", "branch.lonely.tree-parent-branch", "main")  # no fork-commit

        main(["--json"])
        data = json.loads(capsys.readouterr().out)  # must not raise
        by = {b["name"]: b for b in data["branches"]}
        assert by["lonely"]["pending_from_parent"] == 0


class TestExitCodes:
    def test_rebase_no_tree_parent_exits_5(self, repo: RepoHelper) -> None:
        repo.git("branch", "solo")
        repo.checkout("solo")
        with pytest.raises(SystemExit) as exc:
            main(["rebase", "main"])
        assert exc.value.code == 5

    def test_missing_worktree_precondition_exits_4(self, repo: RepoHelper) -> None:
        repo.branch("b", parent="main")  # no worktree
        repo.commit("m2.txt", "m2", "advance")
        with pytest.raises(SystemExit) as exc:
            main(["propagate", "--yes"])
        assert exc.value.code == 4

    def test_argparse_error_exits_2(self, repo: RepoHelper) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["propagate", "--nope"])  # unknown flag
        assert exc.value.code == 2


class TestNoInput:
    def test_propagate_without_yes_errors(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.branch("b", parent="main")
        repo.worktree("b", str(tmp_path / "wt-b"))
        repo.commit("m2.txt", "m2", "advance")
        with pytest.raises(SystemExit) as exc:
            main(["--no-input", "propagate"])
        assert exc.value.code == 4
        assert "--yes" in capsys.readouterr().err

    def test_no_input_with_yes_proceeds(self, repo: RepoHelper, tmp_path) -> None:
        repo.branch("b", parent="main")
        repo.worktree("b", str(tmp_path / "wt-b"))
        repo.commit("m2.txt", "m2", "advance")
        main(["--no-input", "propagate", "--yes"])  # must not raise
        assert "advance" in repo.git("log", "--oneline", "b")

    def test_attach_needs_selection_errors(self, repo: RepoHelper, capsys) -> None:
        repo.git("branch", "solo")
        repo.checkout("solo")
        with pytest.raises(SystemExit) as exc:
            main(["--no-input", "attach"])
        assert exc.value.code == 4
        assert "parent" in capsys.readouterr().err

    def test_no_input_accepted_after_subcommand(self, repo: RepoHelper, tmp_path) -> None:
        # --no-input must work both before AND after the subcommand.
        repo.branch("b", parent="main")
        repo.worktree("b", str(tmp_path / "wt-b"))
        repo.commit("m2.txt", "m2", "advance")
        with pytest.raises(SystemExit) as exc:
            main(["propagate", "--no-input"])  # flag AFTER the subcommand
        assert exc.value.code == 4

    def test_split_needs_after_errors(self, repo: RepoHelper, capsys) -> None:
        repo.commit("c2.txt", "c2", "second commit")  # main now has >= 2 commits
        with pytest.raises(SystemExit) as exc:
            main(["--no-input", "split"])
        assert exc.value.code == 4
        assert "--after" in capsys.readouterr().err


class TestStreamingResults:
    def test_completed_branch_shown_before_conflict(
        self, repo: RepoHelper, capsys, tmp_path
    ) -> None:
        # Two children of main: `a` rebases cleanly, `b` conflicts. `a`'s result must be
        # streamed before `b`'s conflict aborts the cascade.
        repo.commit("shared.txt", "orig", "base")
        repo.branch("a", parent="main")
        wt_a = repo.worktree("a", str(tmp_path / "wt-a"))
        (wt_a / "a.txt").write_text("a")
        repo.git("add", "a.txt", cwd=wt_a)
        repo.git("commit", "-m", "on a", cwd=wt_a)
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))
        (wt_b / "shared.txt").write_text("from b")
        repo.git("add", "shared.txt", cwd=wt_b)
        repo.git("commit", "-m", "b shared", cwd=wt_b)

        repo.checkout("main")
        repo.commit("shared.txt", "from main", "conflict with b, clean for a")

        with pytest.raises(SystemExit):
            cmd_propagate(_prop_ns(yes=True))

        out = capsys.readouterr().out
        assert "Results:" in out
        assert "  a:" in out  # a's completed result printed before b's conflict raised


class TestManpage:
    def test_stdout_is_roff_with_escaped_help(self, capsys) -> None:
        main(["manpage"])
        out = capsys.readouterr().out
        assert out.startswith(".TH GIT-TREE 1\n")
        assert ".SH NAME\n" in out and ".SH DESCRIPTION\n" in out
        assert ".nf\n" in out and out.rstrip().endswith(".fi")
        # Help content is carried verbatim (single source of truth with `-h`).
        assert "Cascading rebase tool" in out
        assert "git tree --json" in out
        # Roff escaping applied: apostrophes in the help ("tree's", "subtree's") become \(aq
        # so man renders them literally, not as typographic quotes; no bare apostrophe or
        # backtick leaks through to be reinterpreted by roff.
        assert "\\(aq" in out
        assert "'" not in out
        assert "`" not in out

    def test_install_writes_file_and_reports_path(self, capsys, tmp_path) -> None:
        man_dir = tmp_path / "man1"
        main(["manpage", "--install", "--dir", str(man_dir)])
        dest = man_dir / "git-tree.1"
        assert dest.is_file()
        assert dest.read_text().startswith(".TH GIT-TREE 1\n")
        assert str(dest) in capsys.readouterr().out

    def test_output_deterministic_across_columns(self, capsys, monkeypatch) -> None:
        # argparse wraps usage/options to COLUMNS; the man page must pin width so its content
        # does not vary with the environment that generates it.
        monkeypatch.setenv("COLUMNS", "40")
        main(["manpage"])
        narrow = capsys.readouterr().out
        monkeypatch.setenv("COLUMNS", "200")
        main(["manpage"])
        wide = capsys.readouterr().out
        assert narrow == wide
        # The ambient COLUMNS is restored after generation, not left clobbered at 80.
        assert os.environ["COLUMNS"] == "200"

    def test_help_epilog_has_for_agents_pointers(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["-h"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "FOR AGENTS" in out
        assert "git tree --json" in out
        assert "AGENTS.md" in out


class TestCompletionGeneration:
    """Completions are generated from the parser (single source of truth), so they can't drift out
    of sync the way the old hand-written strings could. These assert the generated scripts complete
    the right tokens per subcommand, escape correctly, and parse under the real shells."""

    # Options every subparser inherits (the `common` parent + argparse's -h) plus the top-level
    # --all: the completions intentionally never list them per subcommand.
    _UNIVERSAL = {"-h", "--help", "--json", "--no-input", "--all"}

    def _subcommands(self):
        parser = _build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        return sub.choices

    @staticmethod
    def _arm(script: str, name: str) -> str | None:
        """The case-arm body for subcommand `name`, or None if it has no arm."""
        m = re.search(rf"^        {re.escape(name)}\)\n(.*?)\n            ;;", script, re.S | re.M)
        return m.group(1) if m else None

    def test_nonuniversal_flags_appear_in_each_arm(self) -> None:
        for shell in ("zsh", "bash"):
            script = _render_completions(_build_parser(), shell)
            for name, subparser in self._subcommands().items():
                flags = [
                    opt
                    for action in subparser._actions
                    for opt in action.option_strings
                    if opt.startswith("--") and opt not in self._UNIVERSAL
                ]
                arm = self._arm(script, name)
                for flag in flags:
                    assert arm is not None, f"{shell}: no arm for {name}"
                    assert flag in arm, f"{shell}: {name} arm missing flag {flag}"

    def test_value_completers_land_in_the_expected_arms(self) -> None:
        zsh = _render_completions(_build_parser(), "zsh")
        bash = _render_completions(_build_parser(), "bash")
        foreach = "git for-each-ref --format='%(refname:short)' refs/heads/"
        for name in ("propagate", "rebase", "attach", "detach", "remove", "rebuild", "push"):
            assert "__git_heads" in self._arm(zsh, name)
            assert foreach in self._arm(bash, name)
        assert "__git_heads" in self._arm(zsh, "split")  # --after
        assert "_directories" in self._arm(zsh, "split")  # --worktree
        assert "_directories" in self._arm(zsh, "branch")  # path
        assert "compgen -d" in self._arm(bash, "branch")
        assert ":shell:(zsh bash)" in self._arm(zsh, "completions")
        assert 'compgen -W "zsh bash"' in self._arm(bash, "completions")

    def test_yes_flag_uses_the_zsh_exclusion_group(self) -> None:
        zsh = _render_completions(_build_parser(), "zsh")
        for name in ("propagate", "rebase", "detach", "remove", "rebuild", "split", "push"):
            assert "'(-y --yes)'{-y,--yes}" in self._arm(zsh, name)

    def test_universal_and_top_level_flags_are_never_listed(self) -> None:
        for shell in ("zsh", "bash"):
            script = _render_completions(_build_parser(), shell)
            for flag in ("--json", "--no-input", "--all"):
                assert flag not in script, f"{shell} should not list {flag}"

    def test_zsh_escapes_apostrophes_in_descriptions(self) -> None:
        # `remove`'s and split's help contain ASCII apostrophes; inside a single-quoted _describe
        # or option spec they must be escaped ('\''), or a bare ' truncates the spec (and quietly
        # drops later ones; split has two, so the quote count rebalances and `zsh -n` still passes).
        zsh = _render_completions(_build_parser(), "zsh")
        assert "subtree'\\''s worktrees" in zsh  # remove subcommand description
        assert "new branch'\\''s worktree" in zsh  # split --worktree option description
        assert "Don'\\''t create a worktree" in zsh  # split --no-worktree option description

    def test_argless_subcommand_gets_no_arm(self) -> None:
        # `log` has only universal flags, so like the old hand-written script it needs no case arm.
        for shell in ("zsh", "bash"):
            assert "\n        log)\n" not in _render_completions(_build_parser(), shell)

    def test_arms_reference_only_real_subcommands(self) -> None:
        names = set(self._subcommands())
        for shell in ("zsh", "bash"):
            script = _render_completions(_build_parser(), shell)
            for arm in re.findall(r"^        ([a-z-]+)\)$", script, re.M):
                assert arm in names, f"{shell}: arm for unknown subcommand {arm}"

    def test_generated_scripts_parse(self) -> None:
        import shutil
        import subprocess

        for shell in ("bash", "zsh"):
            exe = shutil.which(shell)
            if not exe:
                continue
            script = _render_completions(_build_parser(), shell)
            proc = subprocess.run([exe, "-n"], input=script, capture_output=True, text=True)
            assert proc.returncode == 0, f"{shell} -n rejected the generated script: {proc.stderr}"


class TestDryRun:
    def test_remove_dry_run_removes_nothing(self, repo: RepoHelper, capsys, tmp_path) -> None:
        repo.branch("b", parent="main")
        wt_b = repo.worktree("b", str(tmp_path / "wt-b"))

        main(["remove", "b", "--dry-run"])

        assert wt_b.exists()  # worktree untouched
        assert repo.git("config", "branch.b.tree-parent-branch") == "main"  # config intact
        assert "b" in capsys.readouterr().out  # preview shown
