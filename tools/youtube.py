import typer
import yt_dlp  # for downloading YouTube content


app = typer.Typer()


# Main CLI entry point, allows invocation without subcommand (entry from go.py)
@app.callback()

def youtube(
    url: str = typer.Argument(..., help='YouTube video URL'),
    video: bool = typer.Option(False, '-v', '--video', help='Download best video in mp4'),
    audio: bool = typer.Option(False, '-a', '--audio', help='Download best audio'),
    subtitle: bool = typer.Option(False, '-s', '--subtitle', help='Download subtitles'),
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

    # If no flags, just show metadata
    if not (video or audio or subtitle):
        with yt_dlp.YoutubeDL({}) as ydl:
            info = ydl.extract_info(url, download=False)  # Extract metadata only
            show_meta(info)
        return

    # Set output filename template
    ydl_opts = {'outtmpl': '%(title)s.%(ext)s'}

    # Handle subtitles options if requested
    if subtitle:
        ydl_opts['writesubtitles'] = True
        ydl_opts['writeautomaticsub'] = True
        ydl_opts['subtitleslangs'] = ['en']
        # ydl_opts['subtitlesformat'] = 'srt'  # Uncomment to force srt format

    # Only subtitle (no video/audio)
    if subtitle and not (video or audio):
        ydl_opts['skip_download'] = True

    # Audio only
    elif audio and not video:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
        
    # Video (with audio)
    elif video:
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    # Download requested content and show metadata
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(url, download=True)
        show_meta(result)

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


def show_meta(info):
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


# Entry point for running the script directly
if __name__ == '__main__':
    typer.run(youtube)
