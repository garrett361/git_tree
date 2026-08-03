"""Shell-completion and man-page generation for the git-tree CLI."""

from __future__ import annotations

import argparse
import os

_ZSH_TEMPLATE = """\
#compdef git-tree

_git-tree() {
    local -a subcmds
    subcmds=(
__SUBCMDS__
    )

    if (( CURRENT == 2 )); then
        _describe 'subcommand' subcmds
        return
    fi

    case $words[2] in
__ARMS__
    esac
}

_git-tree "$@"
"""

_BASH_TEMPLATE = """\
_git_tree() {
    local cur prev subcmds
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    subcmds="__SUBCMDS__"

    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$subcmds" -- "$cur"))
        return
    fi

    case "${COMP_WORDS[1]}" in
__ARMS__
    esac
}

complete -F _git_tree git-tree
"""

# Options every subparser inherits (via the `common` parent + argparse's -h); the completions
# intentionally never list them per-command, so the generator skips them.
_UNIVERSAL_OPTS = {"-h", "--help", "--json", "--no-input"}
# zsh completer function per `.completer` tag (set on the arg in _build_parser).
_ZSH_COMPLETER = {"git_heads": "__git_heads", "directories": "_directories"}


def _completable_actions(subparser: argparse.ArgumentParser):
    """A subparser's (options, positionals), minus -h and the universal --json/--no-input."""
    options, positionals = [], []
    for action in subparser._actions:
        if any(opt in _UNIVERSAL_OPTS for opt in action.option_strings):
            continue
        (options if action.option_strings else positionals).append(action)
    return options, positionals


def _arg_label(action: argparse.Action) -> str:
    """The zsh `:message:` label for an arg's value: its metavar if set, else its dest.

    (metavar can be a tuple for multi-metavar args; none of ours are, so fall back to dest.)"""
    return action.metavar if isinstance(action.metavar, str) else action.dest


def _zsh_escape(text: str) -> str:
    """Escape a single-quoted zsh body; only `'` needs it (`'\\''` closes, escapes, reopens)."""
    return text.replace("'", "'\\''")


def _zsh_value(action: argparse.Action) -> str:
    """The zsh action after an arg's `:message:`: a completer, a literal choice set, or empty."""
    completer = getattr(action, "completer", None)
    if completer:
        return _ZSH_COMPLETER[completer]
    if action.choices:
        return "(" + " ".join(action.choices) + ")"
    return ""


def _zsh_spec(action: argparse.Action) -> str:
    """One zsh `_arguments` spec for an option (flag or value-taking) or a positional."""
    desc = _zsh_escape(action.help or "")
    if not action.option_strings:  # positional
        return "':" + _arg_label(action) + ":" + _zsh_value(action) + "'"
    opts = action.option_strings
    if action.nargs == 0:  # a flag
        if len(opts) > 1:  # e.g. -y/--yes: mutually exclusive
            return "'(" + " ".join(opts) + ")'{" + ",".join(opts) + "}'[" + desc + "]'"
        return "'" + opts[0] + "[" + desc + "]'"
    return "'" + opts[0] + "[" + desc + "]:" + _arg_label(action) + ":" + _zsh_value(action) + "'"


def _render_zsh(parser: argparse.ArgumentParser) -> str:
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: a.help or "" for a in getattr(sub, "_choices_actions", [])}
    subcmds = "\n".join(
        f"        '{name}:{_zsh_escape(helps.get(name, ''))}'" for name in sub.choices
    )

    arms = []
    for name, subparser in sub.choices.items():
        options, positionals = _completable_actions(subparser)
        if not options and not positionals:
            continue  # nothing to complete (e.g. `log`): emit no case arm
        specs = [_zsh_spec(a) for a in (*options, *positionals)]
        body = " \\\n                ".join(specs)
        arms.append(
            "        "
            + name
            + ")\n            _arguments \\\n                "
            + body
            + "\n            ;;"
        )

    return _ZSH_TEMPLATE.replace("__SUBCMDS__", subcmds).replace("__ARMS__", "\n".join(arms))


def _bash_value_lines(positionals: list[argparse.Action], indent: str) -> list[str]:
    """Bash lines completing the first positional with a value completer/choices (bash can't switch
    on positional index, so a later positional shares the first's completer)."""
    for action in positionals:
        completer = getattr(action, "completer", None)
        if completer == "git_heads":
            fmt = "--format='%(refname:short)'"
            return [
                indent + f"local branches=$(git for-each-ref {fmt} refs/heads/)",
                indent + 'COMPREPLY=($(compgen -W "$branches" -- "$cur"))',
            ]
        if completer == "directories":
            return [indent + 'COMPREPLY=($(compgen -d -- "$cur"))']
        if action.choices:
            words = " ".join(action.choices)
            return [indent + 'COMPREPLY=($(compgen -W "' + words + '" -- "$cur"))']
    return []


def _render_bash(parser: argparse.ArgumentParser) -> str:
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    arms = []
    for name, subparser in sub.choices.items():
        options, positionals = _completable_actions(subparser)
        if not options and not positionals:
            continue
        optstrings = " ".join(opt for a in options for opt in a.option_strings)
        value_else = _bash_value_lines(positionals, " " * 16)
        if optstrings and value_else:
            lines = [
                '            if [[ "$cur" == -* ]]; then',
                '                COMPREPLY=($(compgen -W "' + optstrings + '" -- "$cur"))',
                "            else",
                *value_else,
                "            fi",
            ]
        elif optstrings:  # flag-only command: complete flags unconditionally
            lines = ['            COMPREPLY=($(compgen -W "' + optstrings + '" -- "$cur"))']
        else:  # positional-only command
            lines = _bash_value_lines(positionals, " " * 12)
        arms.append("        " + name + ")\n" + "\n".join(lines) + "\n            ;;")

    return _BASH_TEMPLATE.replace("__SUBCMDS__", " ".join(sub.choices)).replace(
        "__ARMS__", "\n".join(arms)
    )


def _render_completions(parser: argparse.ArgumentParser, shell: str) -> str:
    """The zsh or bash completion script, derived entirely from the parser (subcommands, flags, help
    text, choices) plus each value-arg's `.completer` tag. Single source of truth with `-h`."""
    return _render_zsh(parser) if shell == "zsh" else _render_bash(parser)


def _set_completer(action: argparse.Action, tag: str) -> None:
    """Tag a value arg with its shell completer (`git_heads`/`directories`), read back via getattr
    in the completion generator. Written through __dict__ so `ty` does not flag it as an unknown
    attribute on argparse.Action."""
    action.__dict__["completer"] = tag


def _render_manpage(parser: argparse.ArgumentParser) -> str:
    """Render a roff man page whose body is the argparse help, verbatim.

    Single source of truth: the same parser feeds `-h`, `--help`, and this page. Width is
    pinned (argparse otherwise wraps usage/options to the generating terminal's width, so the
    output would vary by environment). Installing this page is what makes `git tree --help`
    work: git routes `--help` to `man git-tree`.
    """
    prev_columns = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "80"
    try:
        help_text = parser.format_help()
    finally:
        if prev_columns is None:
            del os.environ["COLUMNS"]
        else:
            os.environ["COLUMNS"] = prev_columns

    # Escape for roff, order matters: backslash first; then literal grave/apostrophe (groff
    # renders bare `/' as typographic quotes, mandoc does not); then neutralize any line a
    # roff parser would read as a request (leading `.` or `'`) so it prints literally.
    lines = []
    for raw in help_text.split("\n"):
        line = raw.replace("\\", "\\e").replace("`", "\\(ga").replace("'", "\\(aq")
        if line[:1] in (".", "'"):
            line = "\\&" + line
        lines.append(line)
    body = "\n".join(lines)

    return (
        ".TH GIT-TREE 1\n"
        ".SH NAME\n"
        "git-tree \\- Cascading rebase tool for branch dependency chains\n"
        ".SH DESCRIPTION\n"
        ".nf\n"
        f"{body}"
        ".fi\n"
    )
