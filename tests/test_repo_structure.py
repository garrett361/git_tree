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

# `__init__.py` is empty and `__main__.py` is a two-line shim whose whole job is to call
# `cli:main`, so it is the single legitimate importer of `cli`. Neither carries code that could
# belong to a layer, and checking `__main__` would only ever report that shim.
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
