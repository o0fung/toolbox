import typer

from tools import tree
from tools import youtube


app = typer.Typer()
app.add_typer(tree.app, name='tree', invoke_without_command=True)
app.add_typer(youtube.app, name='youtube', invoke_without_command=True)


if __name__ == '__main__':
    app()
