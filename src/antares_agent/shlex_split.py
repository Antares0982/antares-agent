"""Decompose a shell command into the individual commands it would run.

The CLI's own deny rules already do this (F24: `;`, `bash -c`, env prefixes,
`cd x;` and subshells were all caught), but the ask-tier lives in our
`can_use_tool` callback, so we have to do the same decomposition ourselves or
the tier is trivially bypassed by `true; git reset --hard`.

This is a heuristic, not a shell. It is the *second* layer -- the sandbox is
the boundary that actually holds -- so it aims to be hard to fool by accident
rather than impossible to fool on purpose.
"""

from __future__ import annotations

import re
import shlex

_WRAPPERS = {"bash", "sh", "zsh", "dash", "env", "nohup", "time", "xargs", "sudo", "doas"}
_ENV_ASSIGN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*=")
#: `shlex`'s own set, plus redirections. A token made only of these characters
#: ends the current command.
_PUNCTUATION = "();<>|&"


def tokenize(command: str) -> list[str]:
    """Split into words *and* operators.

    `shlex.split` is not enough: it treats `;` as an ordinary character, so
    `true; git push` lexes to `['true;', 'git', 'push']` and the ask tier never
    sees a `git push`. `punctuation_chars` makes the operators their own
    tokens while still respecting quotes.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quote. Degrade to whitespace splitting rather than to an
        # empty result -- failing open here would hand the caller a clean bill
        # of health for something we could not read.
        return command.split()


def split_commands(command: str) -> list[list[str]]:
    """Yield argv lists for every command in a shell fragment."""
    tokens = tokenize(command)

    commands: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(c in _PUNCTUATION for c in token):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)

    # shlex drops the quoting around `bash -c '...'`, so the payload arrives as
    # one token; re-split it and splice it in. Same for env prefixes.
    expanded: list[list[str]] = []
    for argv in commands:
        expanded.extend(_unwrap(argv))
    return expanded


def _unwrap(argv: list[str], depth: int = 0) -> list[list[str]]:
    argv = _strip_env_assignments(argv)
    if not argv:
        return []
    if depth >= 4:
        return [argv]

    head = argv[0].rsplit("/", 1)[-1]
    if head in _WRAPPERS:
        for i, token in enumerate(argv[1:], start=1):
            if token == "-c" and i + 1 < len(argv):
                inner = split_commands(argv[i + 1])
                rest = argv[i + 2 :]
                return [argv, *inner, *(_unwrap(rest, depth + 1) if rest else [])]
        rest = argv[1:]
        if rest and not rest[0].startswith("-"):
            # `sudo git push`, `env FOO=1 git push`, `nohup make`
            return [argv, *_unwrap(rest, depth + 1)]
    return [argv]


def _strip_env_assignments(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv) and _ENV_ASSIGN.match(argv[i]):
        i += 1
    return argv[i:]


def matches(argv: list[str], pattern: list[str]) -> bool:
    """Does `argv` start with `pattern`, ignoring the executable's directory?

    Flags are matched anywhere in the command rather than positionally, so
    `git push origin main --force` matches the pattern `git push --force`.
    """
    if not argv or not pattern:
        return False
    words = [p for p in pattern if not p.startswith("-")]
    flags = [p for p in pattern if p.startswith("-")]

    argv = [argv[0].rsplit("/", 1)[-1], *argv[1:]]
    if argv[: len(words)] != words:
        return False
    return all(_has_flag(argv, f) for f in flags)


def _has_flag(argv: list[str], flag: str) -> bool:
    if flag in argv:
        return True
    # `-rf` should satisfy both `-r` and `-f`
    if len(flag) == 2 and flag.startswith("-") and not flag.startswith("--"):
        return any(a.startswith("-") and not a.startswith("--") and flag[1] in a[1:] for a in argv)
    return False
