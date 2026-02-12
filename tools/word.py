"""Top-level command group for word-related utilities."""

from __future__ import annotations

try:
    from ._cli_common import new_typer_app
    from .word_md import register as register_md
    from .word_plot import register as register_plot
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_common import new_typer_app
    from tools.word_md import register as register_md
    from tools.word_plot import register as register_plot


app = new_typer_app()
register_md(app)
register_plot(app)


if __name__ == "__main__":
    app()
