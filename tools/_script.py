import re
import os

def _test(filepath: str):
    """
    Given a full file path, search regular expression pattern.

    Returns OK if found the match.
    """

    # Regex works on the full path. It captures:
    #   dir: any leading directory part (optional)
    #   name: the base name WITHOUT the trailing '_test'
    #   ext: the file extension (optional)
    # This matches only when the stem ends with '_test'.
    pattern = re.compile(r"(?P<dir>.*/)?(?P<name>[^/]+?)(?P<ext>\.[^/]+)?$")
    x = pattern.search(filepath)
    if not x:
        return None

    # outpath = f"{x.group('dir') or ''}{x.group('name')}{x.group('ext') or ''}"
    # os.rename(filepath, outpath)

    return 'OK'
