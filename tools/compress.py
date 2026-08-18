"""File compression commands for `compress`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import typer

try:
    from ._cli_common import new_typer_app
    from ._cli_output import fatal, info
    from ._deps import (
        ensure_binary_or_prompt_install,
        ffmpeg_install_options,
        ghostscript_install_options,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_common import new_typer_app
    from tools._cli_output import fatal, info
    from tools._deps import (
        ensure_binary_or_prompt_install,
        ffmpeg_install_options,
        ghostscript_install_options,
    )


app = new_typer_app(no_args_is_help=True)

_QUALITY_TO_PDFSETTINGS = {
    "screen": "/screen",
    "ebook": "/ebook",
    "printer": "/printer",
    "prepress": "/prepress",
    "default": "/default",
}

_X264_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
}


@app.callback()
def compress() -> None:
    """Compress files such as PDFs and videos."""


@app.command(context_settings={"allow_interspersed_args": True})
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

    input_path = _resolve_existing_file(input_pdf, expected_suffix=".pdf", label="PDF")
    quality_key = _resolve_pdf_quality(quality)
    output_path = _resolve_pdf_output_path(input_path, out)
    _ensure_distinct_output(input_path, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = _build_pdf_command(
        gs_exe=gs_exe,
        input_path=input_path,
        output_path=output_path,
        quality_key=quality_key,
    )
    _run_compression_command(command=command, tool_name="Ghostscript", failure_label="PDF compression")
    _print_compression_summary(
        input_path=input_path,
        output_path=output_path,
        extra_lines=(f"Quality profile: {quality_key}",),
    )


@app.command(context_settings={"allow_interspersed_args": True})
def video(
    input_video: Path = typer.Argument(..., help="Input video path to compress."),
    out: Optional[Path] = typer.Option(
        None,
        "-o",
        "--out",
        help="Output MP4 path. Default: <input_stem>_compressed.mp4 in the same folder.",
    ),
    width: int = typer.Option(
        1280,
        "-w",
        "--width",
        help="Output width in pixels. Height is calculated automatically.",
    ),
    crf: int = typer.Option(
        28,
        "--crf",
        help="x264 quality factor, 0-51. Higher means smaller file and lower quality.",
    ),
    preset: str = typer.Option(
        "slow",
        "-p",
        "--preset",
        help="x264 preset: ultrafast|superfast|veryfast|faster|fast|medium|slow|slower|veryslow|placebo.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Allow replacing an existing output file.",
    ),
) -> None:
    """Compress a video to smaller H.264/AAC MP4 using FFmpeg."""
    ffmpeg_exe = ensure_binary_or_prompt_install(
        binary="ffmpeg",
        missing_message=(
            "FFmpeg executable `ffmpeg` not found on PATH. "
            "Install FFmpeg first (brew/apt/dnf/yum/pacman/zypper/winget/choco)."
        ),
        options=ffmpeg_install_options(),
    )
    if ffmpeg_exe is None:
        fatal("FFmpeg executable `ffmpeg` is required for video compression.")

    input_path = _resolve_existing_file(input_video, expected_suffix=None, label="Video")
    output_path = _resolve_video_output_path(input_path, out)
    _ensure_distinct_output(input_path, output_path)
    _validate_video_options(width=width, crf=crf, preset=preset)
    if output_path.exists() and not overwrite:
        raise typer.BadParameter(f"Output already exists, pass --overwrite to replace it: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = _build_video_command(
        ffmpeg_exe=ffmpeg_exe,
        input_path=input_path,
        output_path=output_path,
        width=width,
        crf=crf,
        preset=preset.strip().lower(),
        overwrite=overwrite,
    )
    _run_compression_command(command=command, tool_name="FFmpeg", failure_label="Video compression")
    _print_compression_summary(
        input_path=input_path,
        output_path=output_path,
        extra_lines=(f"Video settings: width={width}, crf={crf}, preset={preset.strip().lower()}",),
    )


def _resolve_existing_file(path: Path, *, expected_suffix: Optional[str], label: str) -> Path:
    input_path = path.expanduser().resolve()
    if not input_path.is_file():
        raise typer.BadParameter(f"Input {label.lower()} not found: {input_path}")
    if expected_suffix is not None and input_path.suffix.lower() != expected_suffix:
        raise typer.BadParameter(f"Input must be a {expected_suffix} file: {input_path}")
    return input_path


def _resolve_pdf_quality(quality: str) -> str:
    quality_key = quality.strip().lower()
    if quality_key not in _QUALITY_TO_PDFSETTINGS:
        allowed = ", ".join(_QUALITY_TO_PDFSETTINGS.keys())
        raise typer.BadParameter(f"Invalid --quality '{quality}'. Choose from: {allowed}")
    return quality_key


def _validate_video_options(*, width: int, crf: int, preset: str) -> None:
    if width < 2:
        raise typer.BadParameter("--width must be at least 2 pixels.")
    if crf < 0 or crf > 51:
        raise typer.BadParameter("--crf must be between 0 and 51.")
    preset_key = preset.strip().lower()
    if preset_key not in _X264_PRESETS:
        allowed = ", ".join(sorted(_X264_PRESETS))
        raise typer.BadParameter(f"Invalid --preset '{preset}'. Choose from: {allowed}")


def _resolve_pdf_output_path(input_path: Path, out: Optional[Path]) -> Path:
    if out is None:
        return input_path.with_name(f"{input_path.stem}_compressed.pdf")

    candidate = out.expanduser()
    if candidate.suffix.lower() != ".pdf":
        return candidate.with_suffix(".pdf")
    return candidate


def _resolve_video_output_path(input_path: Path, out: Optional[Path]) -> Path:
    if out is None:
        return input_path.with_name(f"{input_path.stem}_compressed.mp4")

    candidate = out.expanduser()
    if candidate.suffix.lower() != ".mp4":
        return candidate.with_suffix(".mp4")
    return candidate


def _ensure_distinct_output(input_path: Path, output_path: Path) -> None:
    if output_path.resolve() == input_path:
        raise typer.BadParameter("Output path must be different from input path.")


def _build_pdf_command(
    *,
    gs_exe: str,
    input_path: Path,
    output_path: Path,
    quality_key: str,
) -> list[str]:
    return [
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


def _build_video_command(
    *,
    ffmpeg_exe: str,
    input_path: Path,
    output_path: Path,
    width: int,
    crf: int,
    preset: str,
    overwrite: bool,
) -> list[str]:
    # FFmpeg flow:
    # 1) Use -y/-n for explicit overwrite behavior before any input is opened.
    # 2) Scale to the requested width and auto-pick an even height (`-2`) for H.264.
    # 3) Encode to widely-compatible MP4 with H.264 video, AAC audio, and faststart metadata.
    return [
        ffmpeg_exe,
        "-y" if overwrite else "-n",
        "-i",
        str(input_path),
        "-vf",
        f"scale={width}:-2",
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def _run_compression_command(*, command: list[str], tool_name: str, failure_label: str) -> None:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        detail = f" {tool_name} error: {stderr}" if stderr else ""
        fatal(f"{failure_label} failed.{detail}")
    except Exception as exc:
        fatal(f"Failed to execute {tool_name}: {exc}")

    _ = result


def _print_compression_summary(
    *,
    input_path: Path,
    output_path: Path,
    extra_lines: tuple[str, ...],
) -> None:
    before_bytes = input_path.stat().st_size
    after_bytes = output_path.stat().st_size
    ratio = (1.0 - (after_bytes / before_bytes)) * 100 if before_bytes > 0 else 0.0

    info(f"Input:  {input_path}")
    info(f"Output: {output_path}")
    for line in extra_lines:
        info(line)
    info(
        f"Size: {_human_size(before_bytes)} -> {_human_size(after_bytes)} "
        f"({ratio:+.1f}% reduction)"
    )


def _human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}PB"


if __name__ == "__main__":
    app()
