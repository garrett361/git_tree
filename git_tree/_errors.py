"""Error types and the machine-readable `error.kind` tag vocabulary."""

from __future__ import annotations

import sys
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class ErrorKind(StrEnum):
    """The `error.kind` value in the JSON error envelope."""

    ERROR = "error"
    USAGE = "usage"
    CONFLICT = "conflict"
    PRECONDITION = "precondition"
    NOT_A_TREE_BRANCH = "not_a_tree_branch"
    INPUT_REQUIRED = "input_required"
    CONFIRMATION_REQUIRED = "confirmation_required"
    LEASE_REJECTED = "lease_rejected"
    UNRESOLVED_CONFLICTS = "unresolved_conflicts"


class TreeError(SystemExit):
    """Raised by helpers to exit with a user-facing message.

    `code` is the process exit status, letting an agent branch on failure class:
    1 generic, 3 resumable conflict, 4 precondition/state, 5 not-a-tree-branch.

    The message is printed to stderr as a human diagnostic (both modes). In `--json`
    mode `main()` also renders these fields into an error envelope on stdout: `kind` is a
    stable machine tag (defaults to a code-derived value), `branches` names the offending
    branches, and `remedy` is an argv list the agent can run directly.
    """

    def __init__(
        self,
        msg: str,
        code: int = 1,
        *,
        kind: ErrorKind | None = None,
        branches: list[str] | None = None,
        remedy: list[str] | None = None,
    ):
        print(msg, file=sys.stderr)
        self.message = msg
        self.kind = kind
        self.branches = branches
        self.remedy = remedy
        super().__init__(code)


class ConflictError(TreeError):
    """A resumable rebase conflict (exit 3). Carries the stuck branch, its worktree, and the
    unmerged files so an agent can resolve and resume without parsing prose."""

    def __init__(
        self,
        msg: str,
        *,
        branch: str,
        worktree: Path,
        conflicted_files: list[str],
        remedy: list[str] | None = None,
    ):
        super().__init__(msg, code=3, kind=ErrorKind.CONFLICT, remedy=remedy)
        self.branch = branch
        self.worktree = worktree
        self.conflicted_files = conflicted_files
