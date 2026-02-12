"""toolbox.tools package."""

from __future__ import annotations

from dataclasses import dataclass

import typer

from . import cheque, clock, tree, word, youtube


@dataclass(frozen=True)
class ToolCommand:
    """Declarative CLI registration entry for a toolbox subcommand."""

    name: str
    app: typer.Typer
    invoke_without_command: bool = False


TOOL_COMMANDS: tuple[ToolCommand, ...] = (
    ToolCommand(name="tree", app=tree.app, invoke_without_command=False),
    ToolCommand(name="youtube", app=youtube.app, invoke_without_command=True),
    ToolCommand(name="clock", app=clock.app, invoke_without_command=True),
    ToolCommand(name="cheque", app=cheque.app, invoke_without_command=True),
    ToolCommand(name="word", app=word.app, invoke_without_command=True),
)


__all__ = [
    "tree",
    "youtube",
    "clock",
    "cheque",
    "word",
    "ToolCommand",
    "TOOL_COMMANDS",
]
