"""Shared console output helpers for CLI tools."""

from __future__ import annotations

from typing import NoReturn

import typer


def info(message: str) -> None:
    """Print a neutral informational message."""
    typer.echo(f"[info] {message}")


def warn(message: str) -> None:
    """Print a warning message."""
    typer.secho(f"[warn] {message}", fg=typer.colors.YELLOW)


def error(message: str, *, err: bool = True) -> None:
    """Print an error message."""
    typer.secho(f"[error] {message}", fg=typer.colors.RED, err=err)


def fatal(message: str, *, code: int = 1, err: bool = True) -> NoReturn:
    """Print an error message and terminate the command."""
    error(message, err=err)
    raise typer.Exit(code=code)
