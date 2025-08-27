
# toolbox

A set of useful command-line tools for enhancing productivity.

## Features

- **tree**: Display a directory tree with optional depth/hidden skipping, plus a flexible batch file renamer that imports your function from a sibling Python file.
- **youtube**: Download YouTube videos, audio, and subtitles, or display video metadata.
- **clock**: Full-screen seven-segment terminal clock with stopwatch and countdown.

## Installation

1. Clone the repository:
	```sh
	git clone https://github.com/o0fung/toolbox.git
	cd toolbox
	```
2. Install via pip (from Git):
	```sh
	pip install "git+https://github.com/o0fung/toolbox.git"
	```

Or for local development:
```sh
pip install -r requirements.txt
```

## Upgrade

Upgrade to the latest from GitHub:
```sh
pip install -U "git+https://github.com/o0fung/toolbox.git"
```

If pip says it's already satisfied or the version hasn't changed, force a reinstall:
```sh
pip install -U --force-reinstall "git+https://github.com/o0fung/toolbox.git"
```

## Usage

Run the installed CLI:
```sh
lf --help
```

Or run via Python entry point in this repo:
```sh
python cli.py [COMMAND] [OPTIONS]
```


---

## 🚩 tree

Display a directory tree.

**Usage:**
```sh
python cli.py tree show [PATH] [--depth DEPTH] [--skip-hidden]
```

- `PATH`: Root directory to display.
- `--depth`, `-d`: Maximum depth to display (default: unlimited).
- `--skip-hidden`, `-s`: Skip hidden files and directories.

### Batch rename helper

Import and execute a function from a Python module located in the same folder as the target file/folder. The function will be called for each discovered file.

Usage:
```sh
# Installed entrypoint
lf tree rename PATH [-m MODULE] [-f FUNC] [-r]

# From the repo
python cli.py tree rename PATH [-m MODULE] [-f FUNC] [-r]
```

Options:
- `PATH` (required): A file or a directory. If a directory, all files in it are processed; with `-r/--recursive`, subdirectories are included.
- `-m, --module` (default: `script`): Python module name (without `.py`) in the same folder as `PATH` to import.
- `-f, --func` (default: `test`): Function name to call inside the module. It should accept a single `filepath: str` argument. If the function returns a non-None value, it will be printed.
- `-r, --recursive`: Recurse into subdirectories when `PATH` is a directory.

Behavior niceties:
- If the module file doesn’t exist, an empty file will be created.
- If the function doesn’t exist in the module, a stub will be appended automatically so you can fill it in.

Example stub and runs:
```py
# In the same folder as your files, create script.py with:
from pathlib import Path

def test(filepath: str):
	p = Path(filepath)
	if p.suffix.lower() == ".txt":
		new = p.with_name(p.stem + "_bak" + p.suffix)
		p.rename(new)
		return f"Renamed: {p.name} -> {new.name}"
```

```sh
# Rename .txt files in a folder (non-recursive)
lf tree rename ~/docs -m script -f test

# Recursive processing
lf tree rename ~/docs -m script -f test -r

# Single file
lf tree rename ~/docs/notes.txt -m script -f test
```

---

## 🚩 youtube

Download YouTube content or show metadata.

**Usage:**
```sh
python cli.py youtube [URL] [--video] [--audio] [--subtitle]
```

- `URL`: YouTube video URL.
- `--video`, `-v`: Download best video in mp4 format.
- `--audio`, `-a`: Download best audio as mp3.
- `--subtitle`, `-s`: Download English subtitles.

If no flags are provided, metadata for the video is displayed.

---

## 🚩 clock

Full-screen digital clock rendered with block characters using Rich. Updates every second.

**Usage:**
```sh
# Clock (from the repo)
python cli.py clock [-c COLOR] [-s SIZE]

# Stopwatch (counts up)
python cli.py clock timer [-c COLOR] [-s SIZE]

# Countdown (flexible input)
#   S           -> seconds
#   H M         -> hours minutes
#   H M S       -> hours minutes seconds
# Non-digit separators like spaces, ':', ',', '/' are accepted.
python cli.py clock countdown 45			// 45 seconds
python cli.py clock countdown 1 10		// 1 minute 10 seconds
python cli.py clock countdown 1 0 1		// 1 hour and 1 second
python cli.py clock countdown 2:15:00 -s xlarge -c magenta		// 2 hours 15 minutes
```

- Press `Ctrl+C` to quit.

Options:
- `--color`, `-c`: Rich color style for digits (e.g., cyan, magenta, "#00ff00").
- `--size`, `-s`: Size preset. One of: `small`, `medium`, `large`, `xlarge`, `xxlarge`, `xxxlarge`.

Notes:
- When running `clock` without a subcommand, the live clock UI starts.
- When running a subcommand (`timer`, `countdown`), the clock UI callback does not run.
- The countdown holds at `00:00:00` until you press `Ctrl+C`.

---

## Project Structure

```
cli.py               # Main CLI entry point
tools/
  tree.py            # Directory tree tool
  youtube.py         # YouTube downloader tool
  clock.py           # Full-screen seven-segment terminal clock
```

## License

See `LICENSE` for details.
