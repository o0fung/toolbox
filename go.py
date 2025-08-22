#!/usr/bin/env python3
import typer

from tools import tree
from tools import youtube
from tools import clock


app = typer.Typer()
app.add_typer(tree.app, name='tree', invoke_without_command=True)
app.add_typer(youtube.app, name='youtube', invoke_without_command=True)
app.add_typer(clock.app, name='clock', invoke_without_command=True)


if __name__ == '__main__':
    app()
