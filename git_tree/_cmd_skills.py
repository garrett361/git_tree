"""The bundled Agent Skills: listing them and installing them as symlinks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from git_tree._errors import TreeError
from git_tree._registry import subcommand
from git_tree._render import _set_completer

if TYPE_CHECKING:
    import argparse


# Per-harness user-scope skill directories, relative to home. Both Claude Code and Codex read the
# agentskills.io layout (`<dir>/<name>/SKILL.md`) and follow directory symlinks at user scope, so
# one bundled copy serves both. Codex's `~/.codex/skills` is deprecated in its source; skip it.
_SKILL_INSTALL_DIRS = (".claude/skills", ".agents/skills")


def _bundled_skills() -> list[Path]:
    """Skill directories shipped in the package, in the agentskills.io layout `<name>/SKILL.md`."""
    root = Path(__file__).parent / "skills"
    if not root.is_dir():
        raise TreeError(f"no bundled skills found at {root}", code=4)
    return sorted(p for p in root.iterdir() if (p / "SKILL.md").is_file())


def _is_git_tree_skill(dest: Path, source: Path) -> bool:
    """Whether `dest` is an entry git-tree installed for `source`, so replacing it is safe.

    Matches the symlink's *stored target* by shape (`.../git_tree/skills/<name>`) rather than
    resolving it and comparing to the current package path. Exact identity would disown git-tree's
    own links the moment the package moves — a renamed clone, a switch between editable and
    non-editable installs, or a uv venv rebuilt under a new Python minor version — and then refuse
    to reinstall over them. Reading the link rather than resolving it also keeps a dangling link
    (target deleted) recognizable, so a reinstall repairs it instead of stalling on it.
    """
    try:
        if not dest.is_symlink():
            return False
        target = dest.readlink()
    except OSError:
        return False  # unreadable or vanished under us: not ours to replace
    return target.parts[-3:] == ("git_tree", "skills", source.name)


def _place_skill(source: Path, dest: Path) -> str:
    """Symlink `source` at `dest`, replacing a previous git-tree install. Returns the action."""
    replaced = dest.is_symlink()
    if replaced:
        dest.unlink()
    dest.symlink_to(source, target_is_directory=True)
    return "Updated" if replaced else "Installed"


def arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--install",
        action="store_true",
        help="Install the skills into ~/.claude/skills and ~/.agents/skills",
    )
    _set_completer(
        p.add_argument(
            "--dir",
            metavar="DIR",
            help="Use DIR instead of the per-harness directories (listing and install alike)",
        ),
        "directories",
    )


@subcommand(
    "skills",
    "List the bundled agent skills; --install links them into your agent harnesses",
    arguments=arguments,
)
def cmd_skills(args: argparse.Namespace) -> dict | None:
    """List the bundled agent skills, or install them into the user's agent harnesses.

    Every filesystem failure becomes a `TreeError`, so `--json` always gets an envelope. Paths
    reach the disk in several places here (resolving `~`, probing a destination, writing a link),
    and pathlib re-raises `EACCES` from even `exists()`/`is_symlink()`, so the whole body is
    guarded rather than the write loop alone. `TreeError` is a `SystemExit`, so the more specific
    errors raised inside pass through untouched.
    """
    try:
        return _install_or_list_skills(args)
    except (OSError, RuntimeError) as err:
        raise TreeError(f"could not read or write the skill directories: {err}", code=4) from err


def _install_or_list_skills(args: argparse.Namespace) -> dict | None:
    skills = _bundled_skills()
    dirs = (
        [Path(args.dir).expanduser()]
        if args.dir
        else [Path.home() / d for d in _SKILL_INSTALL_DIRS]
    )

    if not args.install:
        print("Bundled skills:")
        for skill in skills:
            print(f"  {skill.name}  ({skill})")
        print("\nDestinations:")
        destinations = []
        for d in dirs:
            for skill in skills:
                dest = d / skill.name
                if _is_git_tree_skill(dest, skill):
                    state = "installed"
                elif dest.is_symlink() or dest.exists():
                    state = "occupied by another skill"
                else:
                    state = "not installed"
                print(f"  {dest}  [{state}]")
                destinations.append({"skill": skill.name, "path": str(dest), "state": state})
        print("\nInstall with: git tree skills --install")
        # A query, so it carries the same state the display shows: this listing is how an agent
        # checks what `--install` (a bare mutation) did.
        return {"skills": [s.name for s in skills], "destinations": destinations}

    # Check every destination before writing any: a conflict must leave all of them untouched.
    conflicts = [
        str(dest)
        for d in dirs
        for skill in skills
        if (dest := d / skill.name).is_symlink() or dest.exists()
        if not _is_git_tree_skill(dest, skill)
    ]
    if conflicts:
        raise TreeError(
            "These paths already exist and were not installed by git-tree:\n"
            + "\n".join(f"  {c}" for c in conflicts)
            + "\n\nRemove them, or install elsewhere with --dir DIR. Nothing was written.",
            code=4,
        )

    placed: list[Path] = []
    for d in dirs:
        # Destinations are independent, so a failure partway through leaves earlier ones in place.
        # Name them, so a partial install is recoverable rather than a mystery. (The caller turns
        # any other filesystem error into a TreeError too; this arm only adds the "what landed".)
        try:
            d.mkdir(parents=True, exist_ok=True)
            for skill in skills:
                dest = d / skill.name
                print(f"{_place_skill(skill, dest)} {skill.name} at {dest}")
                placed.append(dest)
        except OSError as err:
            done = "\n".join(f"  {p}" for p in placed) or "  (none)"
            raise TreeError(
                f"Could not install into {d}: {err}\nInstalled before the failure:\n{done}",
                code=4,
            ) from err
    return None
