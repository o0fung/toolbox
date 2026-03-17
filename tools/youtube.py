from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

try:
    from ._cli_common import new_typer_app
    from ._cli_output import error, fatal, info, warn
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_common import new_typer_app
    from tools._cli_output import error, fatal, info, warn


app = new_typer_app()

_METADATA_FIELDS = [
    "title",
    "duration_string",
    "resolution",
    "view_count",
    "comment_count",
    "like_count",
    "channel",
    "channel_follower_count",
    "webpage_url",
    "uploader",
    "upload_date",
    "description",
    "tags",
    "categories",
    "language",
]

_VIDEO_FORMAT_CANDIDATES = (
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
    "bestvideo*+bestaudio*/bestvideo+bestaudio",
    "best",
)


@app.callback()
def youtube(
    url: str = typer.Argument(..., help="YouTube video URL"),
    video: bool = typer.Option(False, "-v", "--video", help="Download best video in mp4"),
    audio: bool = typer.Option(False, "-a", "--audio", help="Download best audio"),
    subtitle: bool = typer.Option(False, "-s", "--subtitle", help="Download subtitles"),
    list_formats: bool = typer.Option(False, "--list", help="List available formats and exit"),
    fmt: Optional[str] = typer.Option(None, "--fmt", help="Explicit yt-dlp format selector (overrides -v/-a logic)"),
    out: Optional[Path] = typer.Option(None, "--out", help="Output directory (default: ~/Desktop)"),
) -> None:
    """Download YouTube content, list formats, or print metadata."""
    if list_formats:
        _print_formats(_extract_info(url, ydl_opts={}, download=False))
        return

    if not (video or audio or subtitle or fmt):
        _show_meta(_extract_info(url, ydl_opts={}, download=False))
        return

    output_dir = _resolve_output_dir(out)
    info(f"Output directory: {output_dir}")

    ydl_opts = _build_ydl_options(
        output_dir=output_dir,
        video=video,
        audio=audio,
        subtitle=subtitle,
        fmt=fmt,
    )
    format_candidates = _select_format_candidates(video=video, fmt=fmt, ydl_opts=ydl_opts)

    try:
        result = _download_with_fallback(url=url, ydl_opts=ydl_opts, format_candidates=format_candidates)
    except RuntimeError as exc:
        fatal(str(exc))

    _show_meta(result)
    download_locations = _resolve_download_locations(result)
    _show_download_summary(video=video, audio=audio, subtitle=subtitle, locations=download_locations)


def _extract_info(url: str, ydl_opts: dict[str, Any], download: bool) -> dict[str, Any]:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=download)


def _resolve_output_dir(out: Optional[Path]) -> Path:
    output_dir = (Path.home() / "Desktop") if out is None else Path(out).expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        fatal(f"Failed creating output directory '{output_dir}': {exc}")
    return output_dir


def _build_ydl_options(
    *,
    output_dir: Path,
    video: bool,
    audio: bool,
    subtitle: bool,
    fmt: Optional[str],
) -> dict[str, Any]:
    options: dict[str, Any] = {"outtmpl": f"{output_dir}/%(title)s.%(ext)s"}

    if subtitle:
        options.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en"],
            }
        )

    if subtitle and not (video or audio):
        options["skip_download"] = True

    if fmt:
        options["format"] = fmt
    elif audio and not video:
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    return options


def _select_format_candidates(
    *,
    video: bool,
    fmt: Optional[str],
    ydl_opts: dict[str, Any],
) -> tuple[str, ...]:
    if video and not fmt:
        return _VIDEO_FORMAT_CANDIDATES
    return (ydl_opts.get("format") or "best",)


def _download_with_fallback(
    *,
    url: str,
    ydl_opts: dict[str, Any],
    format_candidates: tuple[str, ...],
) -> dict[str, Any]:
    skip_download = bool(ydl_opts.get("skip_download", False))
    last_error: Optional[Exception] = None

    for idx, format_expr in enumerate(format_candidates):
        attempt_opts = dict(ydl_opts)
        attempt_opts["format"] = format_expr
        try:
            result = _extract_info(url, ydl_opts=attempt_opts, download=not skip_download)
            if idx > 0:
                info(f"Succeeded with fallback format expression: {format_expr}")
            return result
        except (ExtractorError, DownloadError) as exc:
            last_error = exc
            warn(f"Failed with format '{format_expr}': {exc}")

    error("All format attempts failed.")
    if last_error:
        error(f"Last error: {last_error}")
    raise RuntimeError(
        "Hint: run with --list, then use --fmt <format_id> or a composite selector."
    )


def _show_download_summary(
    *,
    video: bool,
    audio: bool,
    subtitle: bool,
    locations: dict[str, Optional[Path]],
) -> None:
    typer.echo(f">> Downloaded Video : {_status_with_location(video, locations.get('video'))}")
    typer.echo(f">> Downloaded Audio : {_status_with_location(audio, locations.get('audio'))}")
    typer.echo(f">> Downloaded Subtitle : {_status_with_location(subtitle, locations.get('subtitle'))}")


def _status_with_location(enabled: bool, location: Optional[Path]) -> str:
    if not enabled:
        return "None"
    if location is None:
        return "OK"
    return f"OK ({location})"


def _resolve_download_locations(info: dict[str, Any]) -> dict[str, Optional[Path]]:
    paths = _collect_paths_from_info(info)
    video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv"}
    audio_exts = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac"}
    subtitle_exts = {".srt", ".vtt", ".ass", ".ssa", ".ttml", ".srv1", ".srv2", ".srv3"}

    def pick_path(exts: set[str]) -> Optional[Path]:
        for path in paths:
            if path.suffix.lower() in exts:
                return path
        return None

    video_path = pick_path(video_exts)
    audio_path = pick_path(audio_exts)
    subtitle_path = pick_path(subtitle_exts)
    primary_path = paths[0] if paths else None

    if video_path is None:
        video_path = primary_path
    if audio_path is None and primary_path is not None:
        # If audio was merged into the final video output, share that output path.
        audio_path = primary_path

    return {"video": video_path, "audio": audio_path, "subtitle": subtitle_path}


def _collect_paths_from_info(info: dict[str, Any]) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []

    def add_path(raw: Any) -> None:
        if not raw or not isinstance(raw, str):
            return
        candidate = Path(raw).expanduser()
        if candidate in seen:
            return
        seen.add(candidate)
        ordered.append(candidate)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("filepath", "_filename", "filename"):
                if key in node:
                    add_path(node.get(key))

            files_to_move = node.get("__files_to_move")
            if isinstance(files_to_move, dict):
                for source, target in files_to_move.items():
                    add_path(source)
                    add_path(target)

            requested_downloads = node.get("requested_downloads")
            if isinstance(requested_downloads, list):
                for item in requested_downloads:
                    visit(item)

            requested_subtitles = node.get("requested_subtitles")
            if isinstance(requested_subtitles, dict):
                for item in requested_subtitles.values():
                    visit(item)

            entries = node.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    visit(entry)

            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(info)
    return ordered


def _show_meta(info: dict[str, Any]) -> None:
    """Print selected metadata fields from the yt-dlp info dict."""
    typer.echo("================================")
    for key in _METADATA_FIELDS:
        if key == "description":
            typer.echo("description:")
            description = str(info.get("description", ""))
            for line in description.splitlines()[:5]:
                typer.echo(line)
            typer.echo("... [more description]")
            continue
        typer.echo(f"{key}: {info.get(key, '')}")
    typer.echo("================================")


def _print_formats(info: dict[str, Any]) -> None:
    """Pretty-print available formats from info dict."""
    formats = info.get("formats") or []
    typer.echo("Found {} formats. Columns: id  ext  res/fps  vcodec+acodec  size  note".format(len(formats)))
    for fmt_info in formats:
        fid = fmt_info.get("format_id", "")
        ext = fmt_info.get("ext", "")
        height = fmt_info.get("height")
        fps = fmt_info.get("fps")
        res = f"{height}p{'' if not fps else str(fps) + 'fps'}" if height else ""
        vcodec = fmt_info.get("vcodec", "")
        acodec = fmt_info.get("acodec", "")
        size = _human_size(fmt_info.get("filesize") or fmt_info.get("filesize_approx"))
        note = fmt_info.get("format_note", "")
        typer.echo(f"{fid:>6}  {ext:>4}  {res:>10}  {vcodec}+{acodec}  {size:>8}  {note}")
    typer.echo("")
    typer.echo("Examples:")
    typer.echo("  --fmt 251       (audio only opus)")
    typer.echo("  --fmt 137+251   (merge video 1080p mp4 with audio)")
    typer.echo("  -v              (auto fallback logic preferring mp4)")


def _human_size(num: Any) -> str:
    if not num:
        return ""
    value = float(num)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"


if __name__ == "__main__":
    app()
