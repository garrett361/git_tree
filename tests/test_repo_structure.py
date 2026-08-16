"""The layering of `git_tree/` is an invariant, not a convention: this test is what checks it.

`AGENTS.md` gives every module a rank and allows imports only into a strictly lower rank, which is
what keeps the package acyclic. Modules are parsed with `ast` rather than imported: importing them
proves nothing (Python tolerates plenty of cycles at runtime, and a failure would name an
ImportError rather than the offending edge), and it would miss deferred imports entirely.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "git_tree"

RANK = {
    "_errors": 0,
    "_registry": 0,
    "_render": 0,
    "_git": 1,
    "_prompt": 1,
    "_graph": 2,
    "_display": 3,
    "_guards": 3,
    "_engine": 3,
    "_cmd_skills": 3,
    "_cmd_tree": 4,
    "_cmd_branch": 4,
    "_cmd_attach": 4,
    "_cmd_detach": 4,
    "_cmd_remove": 4,
    "_cmd_rebuild": 4,
    "_cmd_propagate": 4,
    "_cmd_rebase": 4,
    "_cmd_split": 4,
    "_cmd_push": 4,
    "_cmd_log": 4,
    "cli": 5,
}

# The one declared exception to "strictly lower": `_engine._skip_empty_commits` calls
# `_guards._refuse_unfinished_replay`. Both sit at L3, and the edge runs one way only.
SAME_RANK_ALLOWED = {("_engine", "_guards")}

# `__init__.py` holds nothing but the side-effect imports that run the `@subcommand` decorators,
# and `__main__.py` is a two-line shim whose whole job is to call `cli:main`, so it is the single
# legitimate importer of `cli`. Neither carries code that could belong to a layer. Since that puts
# `__init__.py` outside the rank checks, `test_package_init_imports_only_command_modules` below
# covers its edges instead.
NOT_LAYERED = {"__init__.py", "__main__.py"}


def _modules() -> dict[str, Path]:
    """Every layered module on disk, by bare name.

    Globbed rather than listed, so a module added later is checked by default: it lands with no
    declared rank and `test_every_module_has_a_declared_rank` says so.
    """
    return {p.stem: p for p in sorted(PACKAGE.glob("*.py")) if p.name not in NOT_LAYERED}


def _imports(path: Path) -> set[str]:
    """The sibling modules `path` imports, by bare name.

    Walks the whole tree rather than the top level, so a function-local import cannot smuggle in a
    back-edge, and counts imports under `if TYPE_CHECKING:` too: a type-only edge is still an edge
    for anyone reading the package, and still closes a cycle on paper.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:  # from ._x import y
                found.add(node.module.split(".")[0])
            elif node.module == "git_tree":  # from git_tree import _x
                found.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith("git_tree."):  # from git_tree._x import y
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:  # import git_tree._x
                if alias.name.startswith("git_tree."):
                    found.add(alias.name.split(".")[1])
    return found


def _edges() -> list[tuple[str, str]]:
    """Every intra-package import as a `(source, target)` pair, in a stable order."""
    return [(src, dst) for src, path in _modules().items() for dst in sorted(_imports(path))]


def test_every_module_has_a_declared_rank() -> None:
    on_disk = set(_modules())
    unranked = sorted(on_disk - set(RANK))
    assert not unranked, (
        f"modules on disk with no declared rank: {unranked}. Give each one a rank in RANK here and "
        "in the layer list in AGENTS.md; until then its imports go unchecked."
    )
    vanished = sorted(set(RANK) - on_disk)
    assert not vanished, (
        f"ranked modules that no longer exist: {vanished}. Drop them from RANK and from AGENTS.md."
    )


def test_imports_only_reach_strictly_lower_ranks() -> None:
    violations = []
    for src, dst in _edges():
        if dst not in RANK:
            violations.append(f"{src}(L{RANK[src]}) -> {dst}(no rank): unknown target")
        elif (src, dst) in SAME_RANK_ALLOWED:
            continue
        elif RANK[dst] > RANK[src]:
            violations.append(f"{src}(L{RANK[src]}) -> {dst}(L{RANK[dst]}): back-edge")
        elif RANK[dst] == RANK[src]:
            violations.append(f"{src}(L{RANK[src]}) -> {dst}(L{RANK[dst]}): undeclared same-rank")
    assert not violations, (
        "imports must go to a strictly lower rank: "
        + "; ".join(violations)
        + ". Move the shared code down a layer rather than adding the edge."
    )


def test_nothing_imports_the_cli_surface() -> None:
    importers = sorted({src for src, dst in _edges() if dst == "cli"})
    assert not importers, (
        f"modules importing cli: {importers}. cli(L5) imports every command module to build the "
        "parser, so any edge back into it is a cycle; the shared name belongs one layer down."
    )


def test_no_command_module_imports_another_command_module() -> None:
    # Strictly stronger than the rank rule, which permits an L4 command reaching `_cmd_skills`(L3).
    violations = [
        f"{src}(L{RANK[src]}) -> {dst}(L{RANK[dst]})"
        for src, dst in _edges()
        if src.startswith("_cmd_") and dst.startswith("_cmd_")
    ]
    assert not violations, (
        "command modules must not import each other: "
        + "; ".join(violations)
        + ". A helper two commands share belongs in _guards or _engine, which is what keeps the "
        "command layer flat."
    )


def _command_module_names() -> set[str]:
    """Subcommand names implied by the files on disk: `_cmd_remove.py` -> `remove`.

    `tree` is excluded: `cmd_tree` is the parser's no-subcommand default
    (`parser.set_defaults(func=cmd_tree)`), not a subcommand, so it is deliberately undecorated
    and absent from `__init__.py`.
    """
    return {p.stem.removeprefix("_cmd_") for p in PACKAGE.glob("_cmd_*.py")} - {"tree"}


def test_every_command_module_is_registered() -> None:
    # Import the package and NOTHING else. Importing the `_cmd_*` modules here would run their
    # decorators during the test, so registration would always succeed and the failure this
    # exists to catch -- a command module missing from `__init__.py`, which is now the only thing
    # tying it into the program -- would be masked.
    import git_tree
    from git_tree._registry import COMMANDS

    assert git_tree  # the import above is the point; keep it from reading as unused
    registered = {c.name for c in COMMANDS}
    missing = sorted(_command_module_names() - registered)
    # Subset, never equality: the registry holds 11 entries after `import git_tree` and 13 once
    # anything in the session has imported `git_tree.cli`, which would make an exact assertion
    # depend on test ordering.
    assert not missing, (
        f"command modules not registered: {missing}. Each needs a @subcommand decorator on its "
        "handler and an import line in git_tree/__init__.py."
    )


def test_every_subparser_dispatches_to_its_own_handler() -> None:
    # `-h` byte-identity cannot catch a mis-wired handler: a decorator naming the wrong command
    # produces identical help text and identical completion scripts. Most commands are never
    # dispatched through main() in the suite either, so assert the mapping directly.
    import argparse
    from importlib import import_module

    from git_tree._cmd_tree import cmd_tree
    from git_tree.cli import _build_parser

    parser = _build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    wrong = []
    for name in sorted(_command_module_names()):
        want = getattr(import_module(f"git_tree._cmd_{name}"), f"cmd_{name}")
        got = sub.choices[name].get_default("func")
        if got is not want:
            wrong.append(f"{name} -> {got!r}, expected {want!r}")
    # These two must set no func of their own: main() dispatches them by name, before the
    # args.func path, because they write non-envelope output and cmd_manpage takes the parser.
    # `set_defaults` on the top-level parser does not propagate onto a subparser, so their own
    # default is None; at parse time `args.func` still resolves to the top-level cmd_tree, which
    # main() never consults for them.
    for name in ("completions", "manpage"):
        got = sub.choices[name].get_default("func")
        if got is not None:
            wrong.append(f"{name} -> {got!r}, expected no func of its own")
    assert not wrong, "subparsers wired to the wrong handler: " + "; ".join(wrong)
    assert parser.parse_args(["completions", "zsh"]).func is cmd_tree


def test_package_init_imports_only_command_modules() -> None:
    # `__init__.py` is in NOT_LAYERED, so the rank test never sees its edges. Harmless when it was
    # empty; now that it holds imports, an `import git_tree.cli` there would be a real cycle.
    imported = _imports(PACKAGE / "__init__.py")
    expected = {f"_cmd_{n}" for n in _command_module_names()}
    assert imported == expected, (
        f"git_tree/__init__.py should import exactly the command modules. "
        f"Missing: {sorted(expected - imported)}. Unexpected: {sorted(imported - expected)}."
    )
