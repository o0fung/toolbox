
# toolbox

A set of useful command-line tools for enhancing productivity.

## Features

- **tree**: Display a directory tree with optional depth and hidden file skipping.
- **youtube**: Download YouTube videos, audio, and subtitles, or display video metadata.
- **clock**: Full-screen seven-segment terminal clock (press Ctrl+C to exit).

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
toolbox --help
```

Or run via Python entry point in this repo:
```sh
python go.py [COMMAND] [OPTIONS]
```


---

## 🚩 tree

Display a directory tree.

**Usage:**
```sh
python go.py tree [PATH] [--depth DEPTH] [--hidden]
```

- `PATH`: Root directory to display.
- `--depth`, `-d`: Maximum depth to display (default: unlimited).
- `--hidden`, `-h`: Skip hidden files and directories.

---

## 🚩 youtube

Download YouTube content or show metadata.

**Usage:**
```sh
python go.py youtube [URL] [--video] [--audio] [--subtitle]
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
# If installed as a package
toolbox clock

# From the repo
python go.py clock
```

- The trailing `.` argument is currently required by the CLI but ignored by the clock.
- Press `Ctrl+C` to quit.

Optional tweak inside code: adjust digit thickness/spacing by changing `inner` and `gap` in `render_big_time()`.

---

## Project Structure

```
go.py                # Main CLI entry point
tools/
  tree.py            # Directory tree tool
  youtube.py         # YouTube downloader tool
	clock.py           # Full-screen seven-segment terminal clock
```

## License

See `LICENSE` for details.
