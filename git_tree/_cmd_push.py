"""The `push` command: push a branch and its descendants to the tree's remote."""

from __future__ import annotations

from typing import TYPE_CHECKING

from git_tree._errors import ErrorKind, TreeError
from git_tree._git import current_branch, git, git_echo, git_lines, git_ok
from git_tree._graph import _root_remote, discover
from git_tree._guards import _require_worktrees
from git_tree._prompt import _proceed
from git_tree._registry import subcommand
from git_tree._render import _set_completer

if TYPE_CHECKING:
    import argparse


def arguments(p: argparse.ArgumentParser) -> None:
    _set_completer(
        p.add_argument("branch", nargs="?", help="Branch to push from (default: current)"),
        "git_heads",
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would be done")
    p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")


@subcommand(
    "push",
    "Push a branch + descendants",
    arguments=arguments,
)
def cmd_push(args: argparse.Namespace) -> dict | None:
    if args.branch is not None:
        branch = args.branch
        # Naming a branch that doesn't exist would otherwise surface as "not a tree-branch",
        # which reads as a tree problem rather than the typo it is.
        if not git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
            raise TreeError(f"No such branch: {branch}", code=4)
    else:
        branch = current_branch()
    graph = discover()

    # Hard-error (unlike cmd_log's benign exit) so a stray `git tree push` on a plain
    # branch like `main` can never force-push it to the branch's own `branch.remote`.
    if branch not in graph.parent_of and branch not in graph.children_of:
        raise TreeError(
            f"{branch} is not a tree-branch." if args.branch else "Not on a tree-branch.",
            code=5,
        )

    descendants = graph.downstream_from(branch)
    push_set = [branch] + descendants
    _require_worktrees([b for b in push_set if b in graph.branches], graph)

    # One remote per tree, defined on the root; every branch pushes there.
    root, root_remote = _root_remote(graph, branch)
    if not root_remote:
        raise TreeError(
            f"root tree-branch '{root}' has no remote configured "
            f"(set it with `git config branch.{root}.remote <remote>`)"
        )

    # Note: intentionally do NOT fetch here. `--force-with-lease` (no explicit
    # expected ref) compares the remote against the remote-tracking ref; fetching
    # first would advance that ref to a teammate's commit and let the force-push
    # silently clobber it. The un-fetched ref reflects our last known state and is
    # exactly what makes the lease reject a clobber.

    stale: list[str] = []
    ahead: dict[str, int] = {}
    new_roots: set[str] = set()

    for b in push_set:
        parent = graph.parent_of.get(b)
        if parent and b != branch:
            merge_base = git("merge-base", parent, b)
            parent_tip = git("rev-parse", parent)
            if merge_base != parent_tip:
                stale.append(b)
                continue

        remote_ref = f"{root_remote}/{b}"
        if git_ok("rev-parse", "--verify", remote_ref):
            ahead[b] = len(git_lines("rev-list", f"{remote_ref}..{b}"))
        elif parent:
            base = git("merge-base", parent, b)
            ahead[b] = len(git_lines("rev-list", f"{base}..{b}"))
        else:
            # Never-pushed root: no remote ref and no parent to count against, so an
            # "ahead" number is meaningless (merge-base(b, b) is b -> a bogus 0). It's new.
            new_roots.add(b)

    pushable = [b for b in push_set if b not in stale]

    # A branch whose ancestor in this run is stale can't be pushed — its base
    # wouldn't be on the remote. Propagate that through descendants (push_set is
    # topological, parents before children) for an accurate preview.
    blocked = set(stale)
    for b in pushable:
        if graph.parent_of.get(b) in blocked:
            blocked.add(b)

    print(f"Pushing to {root_remote} (--force-with-lease):")
    for b in push_set:
        if b in stale:
            print(f"  {b}  (stale - run propagate first)")
        elif b in blocked:
            print(f"  {b}  (skipped - ancestor not pushed)")
        elif b in new_roots:
            print(f"  {b}  (new)")
        else:
            print(f"  {b}  [{ahead.get(b, 0)} ahead]")
    print()

    if args.dry_run or not _proceed(args, "Proceed?"):
        return

    results: list[tuple[str, str]] = []
    failed: list[str] = []
    lease_rejected = False
    for b in pushable:
        # Skip if an ancestor in this run is stale or its push failed. Re-add b so
        # the block cascades to its own descendants later in the loop.
        if graph.parent_of.get(b) in blocked:
            results.append((b, "skipped (ancestor not pushed)"))
            blocked.add(b)
            continue

        res = git_echo("push", "--force-with-lease", "-u", root_remote, b)
        if res.returncode == 0:
            results.append((b, "ok"))
        else:
            blocked.add(b)
            failed.append(b)
            if "stale info" in (res.stderr or ""):
                lease_rejected = True  # the lease caught a remote that moved under us
            results.append((b, "FAILED"))

    print()
    print("Results:")
    for name, status in results:
        print(f"  {name}: {status}")

    if failed:
        # A push that fails must not exit 0 (latent bug). A lease rejection means the remote
        # advanced — fetch + propagate, then retry — which is distinct from a transport/hook
        # failure, so an agent gets a specific `kind`.
        hint = (
            " (lease rejected: the remote moved — fetch, `git tree propagate`, then retry)"
            if lease_rejected
            else ""
        )
        raise TreeError(
            f"push failed for: {', '.join(failed)}{hint}",
            code=1,
            kind=ErrorKind.LEASE_REJECTED if lease_rejected else None,
            branches=failed,
        )

    # Surface what was NOT pushed. This is the one place a bare {ok:true} under-informs an
    # agent: stale/blocked branches are silently left behind and the skip classification isn't
    # cleanly re-derivable from a forest snapshot. (Human mode ignores the return.)
    return {
        "skipped": [{"branch": b, "reason": "stale"} for b in stale]
        + [
            {"branch": b, "reason": "ancestor_not_pushed"}
            for b, status in results
            if status.startswith("skipped")
        ]
    }
