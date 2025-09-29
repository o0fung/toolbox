import typer
import yt_dlp  # for downloading YouTube content
from yt_dlp.utils import ExtractorError
from pathlib import Path


# User can access help message with shortcut -h
app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})


# Main CLI entry point, allows invocation without subcommand (entry from go.py)
@app.callback()
def youtube(
    url: str = typer.Argument(..., help='YouTube video URL'),
    video: bool = typer.Option(False, '-v', '--video', help='Download best video in mp4'),
    audio: bool = typer.Option(False, '-a', '--audio', help='Download best audio'),
    subtitle: bool = typer.Option(False, '-s', '--subtitle', help='Download subtitles'),
    list_formats: bool = typer.Option(False, '--list', help='List available formats and exit'),
    fmt: str = typer.Option(None, '--fmt', help='Explicit yt-dlp format selector (overrides -v/-a logic)'),
    out: Path = typer.Option(None, '--out', help='Output directory (default: ~/Desktop)'),
):
    """
    Download YouTube content using yt_dlp.
    Args:
        url (str): YouTube video URL.
        video (bool): Download best video in mp4 format (includes audio).
        audio (bool): Download best audio and convert to mp3.
        subtitle (bool): Download English subtitles (manual and automatic).
    Behavior:
        - If no flags are provided, displays metadata for the specified YouTube URL.
        - Downloads are saved with the video title as the filename.
        - Displays metadata after download and indicates which content types were downloaded.
    """

    # If user only wants to list formats
    if list_formats:
        with yt_dlp.YoutubeDL({}) as ydl:
            info = ydl.extract_info(url, download=False)
        _print_formats(info)
        return

    # If no download-related flags or fmt, just show metadata
    if not (video or audio or subtitle or fmt):
        with yt_dlp.YoutubeDL({}) as ydl:
            info = ydl.extract_info(url, download=False)  # Extract metadata only
            _show_meta(info)
        return

    # Resolve output directory (default Desktop)
    if out is None:
        output_dir = Path.home() / 'Desktop'
    else:
        output_dir = Path(out).expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        typer.echo(f"[error] Failed creating output directory '{output_dir}': {e}")
        raise typer.Exit(code=1)

    # Set output filename template
    ydl_opts = {'outtmpl': f'{output_dir}/%(title)s.%(ext)s'}
    typer.echo(f"[info] Output directory: {output_dir}")

    # Handle subtitles options if requested
    if subtitle:
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsub'] = True
        ydl_opts['subtitleslangs'] = ['en']
        # ydl_opts['subtitlesformat'] = 'srt'  # Uncomment to force srt format

    # Only subtitle (no video/audio)
    if subtitle and not (video or audio):
        ydl_opts['skip_download'] = True

    # Explicit custom format overrides other flags
    if fmt:
        ydl_opts['format'] = fmt
    # Audio only (unless custom format provided)
    elif audio and not video:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    # Video (with audio) default format logic
    elif video:
        # We'll attempt multiple fallback format expressions below
        pass

    # Download requested content and show metadata
    result = None
    if video and not fmt:
        # Progressive fallback list: prefer mp4, then any best combo, then plain best
        format_candidates = [
            'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'bestvideo*+bestaudio*/bestvideo+bestaudio',
            'best'
        ]
    else:
        # Single attempt (audio, subtitles only, or custom fmt)
        format_candidates = [ydl_opts.get('format') or 'best']

    last_error = None
    for fexpr in format_candidates:
        ydl_opts['format'] = fexpr
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(url, download=not ydl_opts.get('skip_download', False))
            if result:
                if fexpr != format_candidates[0]:
                    print(f"[info] Succeeded with fallback format expression: {fexpr}")
                break
        except ExtractorError as e:
            last_error = e
            print(f"[warn] Failed with format '{fexpr}': {e}")
            continue
    if result is None:
        print("[error] All format attempts failed.")
        if last_error:
            print(f"Last error: {last_error}")
        print("Hint: Run with --list to inspect available formats, then specify one with --fmt <format_id> or composite expression.")
        raise typer.Exit(code=1)

    _show_meta(result)

    # Indicate which content types were downloaded
    print('>> Downloaded Video : ', 'OK' if video else 'None')
    print('>> Downloaded Audio : ', 'OK' if audio else 'None')
    print('>> Downloaded Subtitle : ', 'OK' if subtitle else 'None')


# List of metadata fields to display
params = [
    # 'id',
    'title',
    'duration_string',
    # 'formats',
    # 'thumbnails',
    # 'thumbnail',
    # 'channel_id',
    # 'channel_url',
    # 'duration',
    'resolution',
    'view_count',
    'comment_count',
    'like_count',
    # 'average_rating',
    'channel',
    # 'channel_is_verified',
    'channel_follower_count',
    # 'age_limit',
    'webpage_url',
    # 'playable_in_embed',
    # 'live_status',
    # 'media_type',
    # 'release_timestamp',
    # '_format_sort_fields',
    # 'automatic_captions',
    # 'subtitles',
    # 'chapters',
    # 'heatmap',
    'uploader',
    # 'uploader_id',
    # 'uploader_url',
    'upload_date',
    # 'timestamp',
    # 'availability',
    # 'original_url',
    # 'webpage_url_basename',
    # 'webpage_url_domain',
    # 'extractor',
    # 'extractor_key',
    # 'playlist',
    # 'playlist_index',
    # 'display_id',
    # 'fulltitle',
    # 'release_year',
    # 'is_live',
    # 'was_live',
    # 'requested_subtitles',
    # '_has_drm',
    # 'epoch',
    # 'asr',
    # 'filesize',
    # 'format_id',
    # 'format_note',
    # 'source_preference',
    # 'fps',
    # 'audio_channels',
    # 'height',
    # 'quality',
    # 'has_drm',
    # 'tbr',
    # 'filesize_approx',
    # 'url',
    # 'width',
    # 'language_preference',
    # 'preference',
    # 'ext',
    # 'vcodec',
    # 'acodec',
    # 'dynamic_range',
    # 'downloader_options',
    # 'protocol',
    # 'video_ext',
    # 'audio_ext',
    # 'vbr',
    # 'abr',
    # 'aspect_ratio',
    # 'http_headers',
    # 'format',
    'description',
    'tags',
    'categories',
    'language',
]


def _show_meta(info):
    """
    Display selected metadata fields from the info dict.
    Shows first 5 lines of description, if present.
    """
    print('================================')
    for key in params:
        if key == 'description':
            print(f'{key}:')
            for txt in info.get(key, '').split('\n')[:5]:
                print(txt)
            print('... [more description]')
            continue
        print(f"{key}: {info.get(key, '')}")
    print('================================')


def _print_formats(info):
    """Pretty-print available formats from info dict."""
    formats = info.get('formats') or []
    print(f"Found {len(formats)} formats. Columns: id  ext  res/fps  vcodec+acodec  size  note")
    for f in formats:
        fid = f.get('format_id', '')
        ext = f.get('ext', '')
        height = f.get('height')
        fps = f.get('fps')
        res = (f"{height}p{'' if not fps else str(fps)+'fps'}" if height else '')
        vcodec = f.get('vcodec', '')
        acodec = f.get('acodec', '')
        size = _human_size(f.get('filesize') or f.get('filesize_approx'))
        note = f.get('format_note', '')
        print(f"{fid:>6}  {ext:>4}  {res:>10}  {vcodec}+{acodec}  {size:>8}  {note}")
    print('\nExamples:')
    print('  --fmt 251  (audio only opus)')
    print('  --fmt 137+251  (merge video 1080p mp4 with audio)')
    print('  -v (auto fallback logic preferring mp4)')


def _human_size(num):
    if not num:
        return ''
    for unit in ['B','KB','MB','GB','TB']:
        if num < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


# Entry point for running the script directly
if __name__ == '__main__':
    typer.run(youtube)
