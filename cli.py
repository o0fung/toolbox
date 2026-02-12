#!/usr/bin/env python3
"""Toolbox CLI entry point.

This aggregates subcommands from the tools/ package using Typer.

Subcommands:
    - tree:    filesystem tree viewer + batch processor
    - youtube: youtube/yt-dlp helpers
    - clock:   seven-segment clock, timer, countdown
    - cheque:  HK cheque wording (Traditional Chinese + English)
    - word:    markdown conversion + CSV plotting utilities

Examples:
    python cli.py clock                     # full-screen clock
    python cli.py clock -s large            # choose size preset
    python cli.py clock timer               # stopwatch (counts up)
    python cli.py clock countdown 10        # 10 seconds
    python cli.py clock countdown 1 10      # 1 minute 10 seconds
    python cli.py cheque 123.45             # cheque wording with cents
"""

from tools import TOOL_COMMANDS
from tools._cli_common import new_typer_app


# Root Typer app; expose -h/--help on all levels
app = new_typer_app()

# Mount sub-apps under their command names. With invoke_without_command=True,
# running, e.g., `python cli.py clock` executes clock's default callback.
for command in TOOL_COMMANDS:
    app.add_typer(
        command.app,
        name=command.name,
        invoke_without_command=command.invoke_without_command,
    )


if __name__ == "__main__":
    # Delegate to Typer's CLI runner
    app()
