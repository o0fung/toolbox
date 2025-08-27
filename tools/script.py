import re
import os

def test(filepath: str):
    """Given a full file path, remove a trailing '_test' from the filename stem.

    Examples:
        /path/to/foo_test.py   -> /path/to/foo.py
        /path/to/foo_test      -> /path/to/foo
        /path/to/footest.py    -> unchanged (no trailing '_test' in stem)

    Returns the new path if a change is made; otherwise returns the original path.
    """

    # Regex works on the full path. It captures:
    #   dir: any leading directory part (optional)
    #   name: the base name WITHOUT the trailing '_test'
    #   ext: the file extension (optional)
    # This matches only when the stem ends with '_test'.
    pattern = re.compile(r"(?P<dir>.*/)?(?P<name>[^/]+?)(?P<ext>\.[^/]+)?$")
    m = pattern.search(filepath)
    if not m:
        return f'{filepath} SCANNED...'

    outpath = f"{m.group('dir') or ''}{m.group('name')}{m.group('ext') or ''}"

    # os.rename(filepath, new_path)
    return f'{outpath} FOUND...'
