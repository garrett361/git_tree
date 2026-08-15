"""git-tree: Cascading rebase tool for branch dependency chains."""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import NoReturn

from git_tree._cmd_attach import cmd_attach
from git_tree._cmd_branch import cmd_branch
from git_tree._cmd_detach import cmd_detach
from git_tree._cmd_log import cmd_log
from git_tree._cmd_propagate import cmd_propagate
from git_tree._cmd_push import cmd_push
from git_tree._cmd_rebase import cmd_rebase
from git_tree._cmd_rebuild import cmd_rebuild
from git_tree._cmd_remove import cmd_remove
from git_tree._cmd_skills import cmd_skills
from git_tree._cmd_split import cmd_split
from git_tree._cmd_tree import cmd_tree
from git_tree._errors import ConflictError, ErrorKind, TreeError
from git_tree._render import _render_completions, _render_manpage, _set_completer


def _version() -> str:
    try:
        return metadata.version("git-tree")
    except metadata.PackageNotFoundError:
        return "0+unknown"


def cmd_completions(args: argparse.Namespace) -> None:
    print(_render_completions(_build_parser(), args.shell))


def cmd_manpage(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    roff = _render_manpage(parser)
    if not args.install:
        sys.stdout.write(roff)
        return
    man_dir = Path(args.dir).expanduser() if args.dir else Path.home() / ".local/share/man/man1"
    man_dir.mkdir(parents=True, exist_ok=True)
    dest = man_dir / "git-tree.1"
    dest.write_text(roff)
    print(f"Installed man page to {dest}")


_EPILOG = """\
tree state (git config, no external files):
  branch.<name>.tree-parent-branch   the branch it stacks on
  branch.<name>.tree-fork-commit     where it forks from that parent
  branch.<root>.remote               the tree's single remote (on the root)

FOR AGENTS:
  git tree --json    agent mode: exactly one JSON envelope on stdout, all diagnostics
                     (git echoes, warnings) on stderr; implies --no-input, disables color.
                     The forest query keeps its roots/cycles/orphans/branches keys (each
                     branch also has rebase_in_progress). Mutations return a bare {ok:true}
                     — re-query for post-op state (exceptions: push's `skipped`, and
                     `skills` whose listing is a query and carries per-destination state).
  -y, --yes          skip confirmation on propagate/rebase/push/remove/rebuild/detach;
                     under --json a needed confirm returns kind=confirmation_required
                     (re-run with -y; never auto-confirmed)
  resume a conflict  resolve the files, `git add`, then re-run `git tree propagate <branch>`
                     (the branch you were operating on); it finishes the interrupted rebase
                     (editor disabled) and continues the cascade — no `git rebase --continue`
  --no-input         never prompt; error instead of asking for a value
  --dry-run          preview propagate/rebase/push/remove without mutating
  --version          print git-tree <version>
  exit codes         3 resumable conflict, 4 precondition/state, 5 not-a-tree-branch;
                     error.kind is one of usage/conflict/precondition/not_a_tree_branch/error
                     plus input_required/confirmation_required/lease_rejected/unresolved_conflicts
  full contract      see AGENTS.md in the git-tree source repo
"""


_KIND_BY_CODE = {
    2: ErrorKind.USAGE,
    3: ErrorKind.CONFLICT,
    4: ErrorKind.PRECONDITION,
    5: ErrorKind.NOT_A_TREE_BRANCH,
}


def _envelope(args: argparse.Namespace, data: dict | None = None) -> dict:
    env = {"command": args.command or "tree", "ok": True}
    if data:
        env.update(data)  # flat merge: e.g. cmd_tree's forest keys become siblings
    return env


def _error_envelope(args: argparse.Namespace, err: TreeError) -> dict:
    env = _envelope(args)
    env["ok"] = False
    error: dict = {
        "kind": err.kind or _KIND_BY_CODE.get(err.code, ErrorKind.ERROR),
        "code": err.code,
        "message": err.message,
    }
    if err.branches:
        error["branches"] = err.branches
    if isinstance(err, ConflictError):
        error["branch"] = err.branch
        error["worktree"] = str(err.worktree)
        error["conflicted_files"] = err.conflicted_files
    if err.remedy:
        error["remedy"] = err.remedy
    env["error"] = error
    return env


def _render_error(args: argparse.Namespace, err: TreeError, out) -> NoReturn:
    # The human message is already on stderr (TreeError.__init__). In agent mode, also write
    # the structured envelope to the real stdout so the agent gets exactly one JSON object.
    if args.json:
        print(json.dumps(_error_envelope(args, err), indent=2), file=out)
    raise SystemExit(err.code)


def _build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser. Sole source of truth for the command surface: `main`
    dispatches on it (via each subparser's set_defaults(func=...)), and `_render_manpage`, `-h`,
    and `_render_completions` (both shells) all derive from it. A subcommand added here is wired
    for dispatch, help, the man page, and completions by that one `add_parser` block; a value arg
    that should complete branches or paths is tagged via `_set_completer(..., "git_heads")` (or
    `"directories"`)."""
    parser = argparse.ArgumentParser(
        prog="git-tree",
        description=__doc__,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="With no subcommand, show all trees instead of just the current one",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Agent mode: emit one machine-readable JSON object on stdout (implies --no-input)",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="Never prompt; error (exit 4) if a value would be asked for interactively",
    )
    parser.add_argument("--version", action="version", version=f"git-tree {_version()}")
    # Handler dispatch lives on the parser: the no-subcommand default is cmd_tree, and each
    # dispatchable subparser overrides it via set_defaults(func=...) below. main() calls args.func.
    parser.set_defaults(func=cmd_tree)
    # --no-input is accepted both before the subcommand (top-level, above) and after it
    # (via this shared parent). SUPPRESS on the parent copy means an absent flag leaves the
    # top-level value intact instead of clobbering it with the subparser's default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-input", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    propagate_p = sub.add_parser(
        "propagate", help="Propagate changes to all descendants", parents=[common]
    )
    propagate_p.set_defaults(func=cmd_propagate)
    _set_completer(
        propagate_p.add_argument(
            "branch", nargs="?", help="Branch to propagate from (default: current)"
        ),
        "git_heads",
    )
    propagate_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    propagate_p.add_argument(
        "--no-auto-rerere", action="store_true", help="Disable auto-continue via rerere"
    )
    propagate_p.add_argument(
        "-y", "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    rebase_p = sub.add_parser(
        "rebase", help="Rebase a branch + descendants onto new base", parents=[common]
    )
    rebase_p.set_defaults(func=cmd_rebase)
    _set_completer(
        rebase_p.add_argument("target", help="Branch or ref to rebase onto"), "git_heads"
    )
    _set_completer(
        rebase_p.add_argument("branch", nargs="?", help="Branch to rebase (default: current)"),
        "git_heads",
    )
    rebase_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    rebase_p.add_argument(
        "--no-auto-rerere", action="store_true", help="Disable auto-continue via rerere"
    )
    rebase_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    branch_p = sub.add_parser(
        "branch", help="Create or adopt a child branch with a worktree", parents=[common]
    )
    branch_p.set_defaults(func=cmd_branch)
    _set_completer(
        branch_p.add_argument("path", help="Worktree path for the branch"), "directories"
    )
    branch_p.add_argument("name", help="Branch name (new, or an existing branch to adopt)")
    branch_p.add_argument(
        "--no-submodule-init",
        action="store_true",
        help="Skip automatic `git submodule update --init --recursive` after creating the worktree",
    )

    attach_p = sub.add_parser("attach", help="Attach current branch to tree", parents=[common])
    attach_p.set_defaults(func=cmd_attach)
    _set_completer(
        attach_p.add_argument("parent", nargs="?", help="Parent branch (fzf if omitted)"),
        "git_heads",
    )

    detach_p = sub.add_parser("detach", help="Remove a branch from tree", parents=[common])
    detach_p.set_defaults(func=cmd_detach)
    _set_completer(
        detach_p.add_argument("branch", nargs="?", help="Branch to detach (default: current)"),
        "git_heads",
    )
    detach_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    remove_p = sub.add_parser(
        "remove",
        help="Remove a subtree's worktrees and unregister its branches (keeps refs)",
        parents=[common],
    )
    remove_p.set_defaults(func=cmd_remove)
    _set_completer(
        remove_p.add_argument("branch", nargs="?", help="Branch to remove (default: pick via fzf)"),
        "git_heads",
    )
    remove_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    remove_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    remove_p.add_argument(
        "--force",
        action="store_true",
        help="Remove even if a worktree or its submodules have uncommitted changes",
    )

    rebuild_p = sub.add_parser(
        "rebuild",
        help="Rebuild a corrupted worktree from the branch tip (keeps branch ref and tree config)",
        parents=[common],
    )
    rebuild_p.set_defaults(func=cmd_rebuild)
    _set_completer(
        rebuild_p.add_argument(
            "branch", nargs="?", help="Branch to rebuild (default: pick via fzf)"
        ),
        "git_heads",
    )
    rebuild_p.add_argument(
        "--force", action="store_true", help="Proceed even if worktree has uncommitted changes"
    )
    rebuild_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    split_p = sub.add_parser(
        "split", help="Split current branch into parent + child", parents=[common]
    )
    split_p.set_defaults(func=cmd_split)
    _set_completer(
        split_p.add_argument(
            "--after", metavar="COMMIT", help="Commit to split after (fzf if omitted)"
        ),
        "git_heads",
    )
    split_p.add_argument("--name", metavar="BRANCH", help="New branch name (prompt if omitted)")
    split_p.add_argument(
        "--child",
        action="store_true",
        help="Keep the current branch for the early commits; new branch takes the rest",
    )
    split_wt = split_p.add_mutually_exclusive_group()
    _set_completer(
        split_wt.add_argument(
            "--worktree", metavar="PATH", help="Create the new branch's worktree at PATH"
        ),
        "directories",
    )
    split_wt.add_argument(
        "--no-worktree", action="store_true", help="Don't create a worktree for the new branch"
    )
    split_p.add_argument(
        "-y", "--yes", action="store_true", help="Skip the --child rewind confirmation prompt"
    )

    push_p = sub.add_parser("push", help="Push a branch + descendants", parents=[common])
    push_p.set_defaults(func=cmd_push)
    _set_completer(
        push_p.add_argument("branch", nargs="?", help="Branch to push from (default: current)"),
        "git_heads",
    )
    push_p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    push_p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    log_p = sub.add_parser("log", help="Show git log graph for all tree-branches", parents=[common])
    log_p.set_defaults(func=cmd_log)

    skills_p = sub.add_parser(
        "skills",
        help="List the bundled agent skills; --install links them into your agent harnesses",
        parents=[common],
    )
    skills_p.set_defaults(func=cmd_skills)
    skills_p.add_argument(
        "--install",
        action="store_true",
        help="Install the skills into ~/.claude/skills and ~/.agents/skills",
    )
    _set_completer(
        skills_p.add_argument(
            "--dir",
            metavar="DIR",
            help="Use DIR instead of the per-harness directories (listing and install alike)",
        ),
        "directories",
    )

    completions_p = sub.add_parser(
        "completions", help="Emit shell completion script", parents=[common]
    )
    completions_p.add_argument("shell", choices=["zsh", "bash"], help="Shell type")

    manpage_p = sub.add_parser(
        "manpage",
        help="Emit a man page (roff); --install writes it to the man path",
        parents=[common],
    )
    manpage_p.add_argument(
        "--install",
        action="store_true",
        help="Write the man page under the man path instead of printing to stdout",
    )
    _set_completer(
        manpage_p.add_argument(
            "--dir",
            metavar="DIR",
            help="Directory to install into (default: ~/.local/share/man/man1)",
        ),
        "directories",
    )
    return parser


def main(argv: list[str] | None = None) -> None:  # explicit argv for tests
    parser = _build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown and args.command != "log":
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    if args.command == "log":
        args.extra = unknown

    # Single output edge. In agent mode all inline human/`git_echo` output is redirected to
    # stderr (a stdlib context manager, restored on exit) so stdout carries exactly one JSON
    # object, written after the handler returns. Errors render here too, in one place.
    agent = args.json
    real_stdout = sys.stdout
    try:
        if args.command == "manpage":
            # Emits roff (or installs) to the real stdout; --json is not meaningful here.
            cmd_manpage(args, parser)
        elif args.command == "completions":
            cmd_completions(args)  # shell script to real stdout; --json not meaningful
        elif args.command == "log" and agent:
            raise TreeError(
                "`git tree log` has no JSON form; query state with `git tree --json`.", code=2
            )
        else:
            # manpage/completions are handled above (pre-dispatch); every other subparser sets
            # func, and the top-level default covers the no-subcommand case.
            handler = args.func
            with contextlib.redirect_stdout(sys.stderr) if agent else contextlib.nullcontext():
                # Queries return envelope data (cmd_tree's forest, cmd_skills' listing); mutations
                # return None and stay bare, except cmd_push's non-re-derivable skip set.
                data = handler(args)
            if agent:
                print(json.dumps(_envelope(args, data), indent=2), file=real_stdout)
    except subprocess.CalledProcessError as e:
        # A bare git() (check=True) failed somewhere unexpected. Surface git's own command
        # and stderr as a clean error instead of dumping a CalledProcessError traceback.
        cmd = " ".join(e.cmd) if isinstance(e.cmd, (list, tuple)) else str(e.cmd)
        stderr = (e.stderr or "").strip()
        _render_error(
            args,
            TreeError(
                f"git command failed (exit {e.returncode}): {cmd}"
                + (f"\n{stderr}" if stderr else "")
            ),
            real_stdout,
        )
    except TreeError as e:
        _render_error(args, e, real_stdout)


if __name__ == "__main__":
    main()
