"""Importing the command modules runs their @subcommand decorators, which is what populates
_registry.COMMANDS for _build_parser. These imports exist for that side effect and are
intentionally unreferenced; _cmd_tree is absent because cmd_tree is the no-subcommand default
rather than a subcommand, and cli.py imports it directly."""

import git_tree._cmd_attach
import git_tree._cmd_branch
import git_tree._cmd_detach
import git_tree._cmd_log
import git_tree._cmd_propagate
import git_tree._cmd_push
import git_tree._cmd_rebase
import git_tree._cmd_rebuild
import git_tree._cmd_remove
import git_tree._cmd_skills
import git_tree._cmd_split
