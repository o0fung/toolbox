#!/usr/bin/env python3
"""Toolbox CLI entry point.

This aggregates subcommands from the tools/ package using Typer.

Subcommands:
    - tree:    filesystem tree viewer
    - youtube: youtube/yt-dlp helpers
    - clock:   seven-segment clock, timer, countdown

Examples:
    py go.py clock                     # full-screen clock
    py go.py clock -s large            # choose size preset
    py go.py clock timer               # stopwatch (counts up)
    py go.py clock countdown 10        # 10 seconds
    py go.py clock countdown 1 10      # 1 minute 10 seconds (flex separators)
    py go.py clock countdown 1 0 1     # 1 hour 0 minute 1 second
    py go.py clock countdown 2:15:00 -s xlarge -c magenta
"""

import typer

from tools import tree
from tools import youtube
from tools import clock


# Root Typer app; expose -h/--help on all levels
app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})

# Mount sub-apps under their command names. With invoke_without_command=True,
# running, e.g., `py cli.py clock` executes clock's default callback.
app.add_typer(tree.app, name='tree', invoke_without_command=True)
app.add_typer(youtube.app, name='youtube', invoke_without_command=True)
app.add_typer(clock.app, name='clock', invoke_without_command=True)


if __name__ == '__main__':
    # Delegate to Typer's CLI runner
    app()
