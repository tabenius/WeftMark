"""Shell completions generated from the live argparse tree.

Nothing here opens a repository or a ledger: a completion script must be safe to
source from any directory. Adding a subcommand or option to ``build_parser`` is
enough for it to appear in both bash and fish completions.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Option:
    flags: tuple[str, ...]
    help: str
    takes_value: bool
    choices: tuple[str, ...] = ()

    @property
    def long(self) -> str | None:
        return next((flag for flag in self.flags if flag.startswith("--")), None)

    @property
    def short(self) -> str | None:
        return next((flag for flag in self.flags if flag.startswith("-") and not flag.startswith("--")), None)


@dataclass(frozen=True)
class Command:
    path: tuple[str, ...]
    help: str
    options: tuple[Option, ...]
    positional_choices: tuple[tuple[str, ...], ...]
    children: tuple["Command", ...]

    @property
    def key(self) -> str:
        return " ".join(self.path)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _walk(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> Command:
    options: list[Option] = []
    choices: list[tuple[str, ...]] = []
    children: list[Command] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                children.append(_walk(subparser, path + (str(name),)))
        elif action.option_strings:
            options.append(
                Option(
                    flags=tuple(str(flag) for flag in action.option_strings),
                    help=_clean(action.help),
                    takes_value=action.nargs != 0,
                    choices=tuple(str(c) for c in (action.choices or ())),
                )
            )
        elif action.choices:
            choices.append(tuple(str(c) for c in action.choices))
    return Command(path, _clean(parser.description), tuple(options), tuple(choices), tuple(children))


def command_tree(parser: argparse.ArgumentParser) -> Command:
    """Root command node; recursively contains every subparser."""
    return _walk(parser)


def _flatten(root: Command) -> Iterable[Command]:
    yield root
    for child in root.children:
        yield from _flatten(child)


def _bash_case(name: str, cases: list[tuple[str, list[str]]]) -> list[str]:
    lines = [f"{name}() {{", '    case "$1" in']
    for key, values in cases:
        rendered = " ".join(values)
        lines.append(f'        "{key}" ) printf "%s\\n" {rendered} ;;')
    lines += ['        * ) : ;;', "    esac", "}", ""]
    return lines


def bash_script(parser: argparse.ArgumentParser) -> str:
    nodes = list(_flatten(command_tree(parser)))
    children = [(node.key, [child.path[-1] for child in node.children]) for node in nodes]
    value_opts = [
        (node.key, [flag for opt in node.options if opt.takes_value for flag in opt.flags])
        for node in nodes
    ]
    words = [
        (
            node.key,
            [child.path[-1] for child in node.children]
            + [choice for group in node.positional_choices for choice in group]
            + [flag for opt in node.options for flag in opt.flags]
            + [choice for opt in node.options for choice in opt.choices],
        )
        for node in nodes
    ]
    lines = [
        "# weftmark bash completion.",
        '# Setup: eval "$(weftmark completion bash)"  (add to ~/.bashrc), or',
        "#   weftmark completion bash | sudo tee /etc/bash_completion.d/weftmark",
        "",
    ]
    lines += _bash_case("_weftmark_children", children)
    lines += _bash_case("_weftmark_value_opts", value_opts)
    lines += _bash_case("_weftmark_words", words)
    lines += [
        "_weftmark_complete() {",
        '    local cur="${COMP_WORDS[COMP_CWORD]}"',
        '    local path="" word i=1',
        '    while [ "$i" -lt "$COMP_CWORD" ]; do',
        '        word="${COMP_WORDS[$i]}"',
        "        case \"$word\" in",
        "            -*)",
        "                if _weftmark_value_opts \"$path\" | grep -Fxq -- \"$word\"; then",
        "                    i=$((i + 1))",
        "                fi",
        "                ;;",
        "            *)",
        '                if _weftmark_children "$path" | grep -Fxq -- "$word"; then',
        '                    if [ -z "$path" ]; then path="$word"; else path="$path $word"; fi',
        "                fi",
        "                ;;",
        "        esac",
        "        i=$((i + 1))",
        "    done",
        '    COMPREPLY=( $(compgen -W "$(_weftmark_words "$path")" -- "$cur") )',
        "}",
        "complete -F _weftmark_complete weftmark",
        "",
    ]
    return "\n".join(lines)


def _fish_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ").replace('"', '\\"')


def _fish_guard(path: tuple[str, ...]) -> str:
    if not path:
        return "__fish_use_subcommand"
    seen = "; and ".join(f"__fish_seen_subcommand_from {name}" for name in path)
    return seen


def fish_script(parser: argparse.ArgumentParser) -> str:
    root = command_tree(parser)
    lines = [
        "# weftmark fish completion.",
        "# Setup: weftmark completion fish > ~/.config/fish/completions/weftmark.fish",
        "complete -c weftmark -f",
    ]
    for node in _flatten(root):
        guard = _fish_guard(node.path)
        for child in node.children:
            description = f" -d '{_fish_quote(child.help)}'" if child.help else ""
            lines.append(
                f"complete -c weftmark -n {guard} -a {child.path[-1]}{description}"
            )
        for group in node.positional_choices:
            lines.append(
                f"complete -c weftmark -n '{guard}' -a '{' '.join(group)}'"
            )
        for opt in node.options:
            long = opt.long
            if not long:
                continue
            pieces = [f"complete -c weftmark -n '{guard}' -l {long[2:]}"]
            if opt.short:
                pieces.append(f"-s {opt.short[1:]}")
            if opt.takes_value:
                pieces.append("-r")
            if opt.choices:
                pieces.append(f"-a '{' '.join(opt.choices)}'")
            if opt.help:
                pieces.append(f"-d '{_fish_quote(opt.help)}'")
            lines.append(" ".join(pieces))
    return "\n".join(lines) + "\n"


def script(shell: str, parser: argparse.ArgumentParser) -> str:
    if shell == "bash":
        return bash_script(parser)
    if shell == "fish":
        return fish_script(parser)
    raise ValueError(f"unsupported shell: {shell}")
