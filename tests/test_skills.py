from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path

import pytest

from git_tree._cmd_skills import _bundled_skills
from git_tree.cli import _build_parser, main

SKILL_NAMES = {"git-tree-land", "git-tree-doctor", "git-tree-propagate", "git-tree-plan"}


def _foreign_skill(path: Path) -> None:
    """A skill directory at `path` that git-tree did not install."""
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: someone-elses\ndescription: not ours\n---\n\nhi\n")


class TestInstall:
    def test_symlinks_resolve_to_the_bundled_directories(self, capsys, tmp_path) -> None:
        main(["skills", "--install", "--dir", str(tmp_path)])
        for source in _bundled_skills():
            dest = tmp_path / source.name
            assert dest.is_symlink()
            assert dest.resolve() == source.resolve()
            assert (dest / "SKILL.md").is_file()
        assert "Installed" in capsys.readouterr().out

    def test_reinstall_is_idempotent(self, capsys, tmp_path) -> None:
        main(["skills", "--install", "--dir", str(tmp_path)])
        capsys.readouterr()
        main(["skills", "--install", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert "Updated" in out
        for source in _bundled_skills():
            assert (tmp_path / source.name).resolve() == source.resolve()

    def test_install_returns_a_bare_envelope(self, capsys, tmp_path) -> None:
        main(["--json", "skills", "--install", "--dir", str(tmp_path)])
        assert json.loads(capsys.readouterr().out) == {"command": "skills", "ok": True}

    def test_listing_reports_state_per_destination(self, capsys, tmp_path) -> None:
        main(["--json", "skills", "--dir", str(tmp_path)])
        before = json.loads(capsys.readouterr().out)
        assert {d["state"] for d in before["destinations"]} == {"not installed"}

        main(["skills", "--install", "--dir", str(tmp_path)])
        capsys.readouterr()
        main(["--json", "skills", "--dir", str(tmp_path)])
        after = json.loads(capsys.readouterr().out)
        assert {d["state"] for d in after["destinations"]} == {"installed"}
        assert {d["skill"] for d in after["destinations"]} == SKILL_NAMES

    def test_installs_into_both_harness_directories_by_default(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        main(["skills", "--install"])
        for parent in (tmp_path / ".claude/skills", tmp_path / ".agents/skills"):
            for source in _bundled_skills():
                assert (parent / source.name).resolve() == source.resolve()

    def test_unwritable_destination_reports_what_landed(
        self, capsys, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".agents").write_text("not a directory")
        with pytest.raises(SystemExit) as exc:
            main(["skills", "--install"])
        assert exc.value.code == 4
        err = capsys.readouterr().err
        assert "Installed before the failure" in err
        for name in SKILL_NAMES:
            assert str(tmp_path / ".claude/skills" / name) in err

    def test_filesystem_failure_is_an_error_envelope_not_a_traceback(
        self, capsys, tmp_path
    ) -> None:
        """Every filesystem failure funnels through one guard in `cmd_skills`, so one case
        (a --dir that is a file) covers the unreadable-parent and bad-`~` cases too."""
        target = tmp_path / "afile"
        target.write_text("x")
        with pytest.raises(SystemExit) as exc:
            main(["--json", "skills", "--install", "--dir", str(target)])
        assert exc.value.code == 4
        env = json.loads(capsys.readouterr().out)
        assert env["ok"] is False
        assert env["error"]["kind"] == "precondition"


class TestOwnership:
    @pytest.mark.parametrize("old_clone_exists", [True, False], ids=["moved", "deleted"])
    def test_link_from_a_relocated_package_is_still_ours(self, tmp_path, old_clone_exists) -> None:
        """A symlink git-tree wrote stays recognizable after the package moves, dangling or not.

        Exact-path ownership would disown these and refuse to reinstall over them.
        """
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        for source in _bundled_skills():
            old = tmp_path / "old-clone" / "git_tree" / "skills" / source.name
            if old_clone_exists:
                old.mkdir(parents=True)
            (dest_dir / source.name).symlink_to(old, target_is_directory=True)

        main(["skills", "--install", "--dir", str(dest_dir)])
        for source in _bundled_skills():
            assert (dest_dir / source.name / "SKILL.md").is_file()


class TestRefusesToClobber:
    def test_foreign_entry_is_refused(self, capsys, tmp_path) -> None:
        foreign = tmp_path / "git-tree-land"
        _foreign_skill(foreign)
        with pytest.raises(SystemExit) as exc:
            main(["skills", "--install", "--dir", str(tmp_path)])
        assert exc.value.code == 4
        assert str(foreign) in capsys.readouterr().err

    def test_symlink_pointing_outside_the_package_is_refused(self, tmp_path) -> None:
        elsewhere = tmp_path / "elsewhere"
        _foreign_skill(elsewhere)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        (dest_dir / "git-tree-land").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(SystemExit) as exc:
            main(["skills", "--install", "--dir", str(dest_dir)])
        assert exc.value.code == 4
        assert (dest_dir / "git-tree-land").resolve() == elsewhere.resolve()

    def test_nothing_is_written_when_one_destination_conflicts(self, tmp_path) -> None:
        foreign = tmp_path / "git-tree-land"
        _foreign_skill(foreign)
        original = (foreign / "SKILL.md").read_text()
        with pytest.raises(SystemExit):
            main(["skills", "--install", "--dir", str(tmp_path)])
        assert (foreign / "SKILL.md").read_text() == original
        assert not (tmp_path / "git-tree-doctor").exists()


class TestBundledContent:
    def test_frontmatter_is_spec_conformant(self) -> None:
        """agentskills.io requires `name` equal to the directory name and a non-empty
        `description` of at most 1024 chars. Both harnesses read exactly these two fields."""
        for source in _bundled_skills():
            front = _frontmatter(source / "SKILL.md")
            assert front["name"] == source.name
            assert front["description"]
            assert len(front["description"]) <= 1024

    def test_documented_commands_parse(self) -> None:
        """Every `git tree ...` in the shipped skills must still be a real invocation.

        Same motivation as the generated-completions tests: instructions that ship to another
        machine must not outlive a renamed flag or subcommand.
        """
        parser = _build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        valid = set(sub.choices)
        found = 0
        for source in _bundled_skills():
            for argv in _invocations(source / "SKILL.md"):
                found += 1
                if not argv[0].startswith("-"):
                    assert argv[0] in valid, f"{source.name}: unknown subcommand {argv[0]}"
                if len(argv) == 1:
                    continue  # a bare mention of the command, not a full invocation
                _, unknown = parser.parse_known_args(argv)
                assert unknown == [], f"{source.name}: unrecognized {unknown} in {argv}"
        assert found > 10


def _frontmatter(skill_md: Path) -> dict[str, str]:
    lines = skill_md.read_text().splitlines()
    assert lines[0].strip() == "---", f"{skill_md} has no frontmatter"
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    raise AssertionError(f"{skill_md} frontmatter is unterminated")


def _invocations(skill_md: Path) -> list[list[str]]:
    """Every `git tree ...` command in a skill, as argv without the `git tree` prefix.

    Only code is collected, inline spans and fenced blocks, so prose that happens to name the
    tool ("when git tree reports orphans") is not mistaken for an invocation.
    """
    text = skill_md.read_text()
    raw = [m.group(1) for m in re.finditer(r"`git tree ([^`\n]+)`", text)]
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            _, sep, rest = line.partition("git tree ")
            if sep:
                raw.append(rest)

    argvs = []
    for command in raw:
        command = command.partition("#")[0].strip().rstrip(".,;:")
        if command:
            argvs.append(shlex.split(command))
    return argvs
