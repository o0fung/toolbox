"""PDF compression command implementation for `pdf`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import typer

try:
    from ._deps import ensure_binary_or_prompt_install, ghostscript_install_options
    from ._cli_common import new_typer_app
    from ._cli_output import fatal, info
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._deps import ensure_binary_or_prompt_install, ghostscript_install_options
    from tools._cli_common import new_typer_app
    from tools._cli_output import fatal, info


app = new_typer_app(context_settings={"allow_interspersed_args": True})

_QUALITY_TO_PDFSETTINGS = {
    "screen": "/screen",
    "ebook": "/ebook",
    "printer": "/printer",
    "prepress": "/prepress",
    "default": "/default",
}


@app.callback()
def pdf(
    input_pdf: Path = typer.Argument(..., help="Input PDF path to compress."),
    out: Optional[Path] = typer.Option(
        None,
        "-o",
        "--out",
        help="Output PDF path. Default: <input_stem>_compressed.pdf in the same folder.",
    ),
    quality: str = typer.Option(
        "ebook",
        "-q",
        "--quality",
        help="Compression profile: screen|ebook|printer|prepress|default.",
    ),
) -> None:
    """Compress a PDF file using Ghostscript."""
    gs_exe = ensure_binary_or_prompt_install(
        binary="gs",
        missing_message=(
            "Ghostscript executable `gs` not found on PATH. "
            "Install Ghostscript first (brew/apt/dnf/yum/pacman/zypper/winget/choco)."
        ),
        options=ghostscript_install_options(),
    )
    if gs_exe is None:
        fatal("Ghostscript executable `gs` is required for PDF compression.")

    input_path = input_pdf.expanduser().resolve()
    if not input_path.is_file():
        raise typer.BadParameter(f"Input PDF not found: {input_path}")
    if input_path.suffix.lower() != ".pdf":
        raise typer.BadParameter(f"Input must be a .pdf file: {input_path}")

    quality_key = quality.strip().lower()
    if quality_key not in _QUALITY_TO_PDFSETTINGS:
        allowed = ", ".join(_QUALITY_TO_PDFSETTINGS.keys())
        raise typer.BadParameter(f"Invalid --quality '{quality}'. Choose from: {allowed}")

    output_path = _resolve_output_path(input_path, out)
    if output_path.resolve() == input_path:
        raise typer.BadParameter("Output path must be different from input path.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        gs_exe,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={_QUALITY_TO_PDFSETTINGS[quality_key]}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        f"-sOutputFile={str(output_path)}",
        str(input_path),
    ]

    # Compression flow:
    # 1) Run Ghostscript once with selected quality profile.
    # 2) Surface stderr context on failures to make terminal debugging practical.
    # 3) Report before/after size and reduction ratio for quick verification.
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        detail = f" Ghostscript error: {stderr}" if stderr else ""
        fatal(f"PDF compression failed.{detail}")
    except Exception as exc:
        fatal(f"Failed to execute Ghostscript: {exc}")

    _ = result
    before_bytes = input_path.stat().st_size
    after_bytes = output_path.stat().st_size
    ratio = (1.0 - (after_bytes / before_bytes)) * 100 if before_bytes > 0 else 0.0

    info(f"Input:  {input_path}")
    info(f"Output: {output_path}")
    info(f"Quality profile: {quality_key}")
    info(
        f"Size: {_human_size(before_bytes)} -> {_human_size(after_bytes)} "
        f"({ratio:+.1f}% reduction)"
    )


def _resolve_output_path(input_path: Path, out: Optional[Path]) -> Path:
    if out is None:
        return input_path.with_name(f"{input_path.stem}_compressed.pdf")

    candidate = out.expanduser()
    if candidate.suffix.lower() != ".pdf":
        return candidate.with_suffix(".pdf")
    return candidate


def _human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}PB"


if __name__ == "__main__":
    app()
