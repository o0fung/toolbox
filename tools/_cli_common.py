"""Shared CLI helpers for toolbox commands."""

from __future__ import annotations

from typing import Any

import typer

HELP_OPTION_NAMES = ("-h", "--help")


def new_typer_app(**kwargs: Any) -> typer.Typer:
    """Create a Typer app with consistent help flag shortcuts."""
    context_settings = dict(kwargs.pop("context_settings", {}) or {})
    context_settings.setdefault("help_option_names", list(HELP_OPTION_NAMES))
    return typer.Typer(context_settings=context_settings, **kwargs)
