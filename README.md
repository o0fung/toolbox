
# toolbox

A set of useful command-line tools for enhancing productivity.

## Features

- **tree**: Display a directory tree with optional depth/hidden skipping, plus a flexible batch file processor that imports your function from a sibling Python file.
- **youtube**: Download YouTube videos, audio, and subtitles, or display video metadata.
- **clock**: Full-screen seven-segment terminal clock with stopwatch and countdown.
- **cheque**: Convert integer amounts to formal HK cheque wording in Traditional Chinese and English.
- **word**: Plot CSV data with pyqtgraph subplots; auto-detect time/index X-axis and plot all data-like columns.

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
python cli.py tree show PATH [--depth DEPTH] [--skip-hidden]
```

- `PATH`: Root directory to display.
- `--depth`, `-d`: Maximum depth to display (default: unlimited).
- `--skip-hidden`, `-s`: Skip hidden files and directories.

### Batch file processor

Import and execute a function from a Python module located in the same folder as the target file/folder. The function will be called for each discovered file.

Usage:
```sh
# Installed entrypoint
lf tree work PATH [-d DEPTH] [--skip-hidden] [-m MODULE] [-f FUNC]

# From the repo
python cli.py tree work PATH [-d DEPTH] [--skip-hidden] [-m MODULE] [-f FUNC]
```

Options:
- `PATH` (required): A file or a directory. If a directory, items are processed up to the specified depth.
- `-d, --depth` (default: 1): Maximum depth when displaying/processing.
- `-s, --skip-hidden`: Skip hidden/special files (dot- or underscore-prefixed).
- `-m, --module` (default: `_script`): Python module name (without `.py`) in the same folder as `PATH` to import.
- `-f, --func` (default: `_test`): Function name to call inside the module. It should accept a single `filepath: str` argument. If the function returns a non-None string, it will be shown on that file’s line.

Behavior niceties:
- If the module file doesn’t exist, a template is copied from `tools/_script.py` to help you start quickly.
- If the function doesn’t exist in the module, a stub will be appended automatically so you can fill it in.

Example stub and runs:
```py
# In the same folder as your files, create _script.py with:
from pathlib import Path

def _test(filepath: str):
	p = Path(filepath)
	if p.suffix.lower() == ".txt":
		new = p.with_name(p.stem + "_bak" + p.suffix)
		p.rename(new)
		return f"Renamed: {p.name} -> {new.name}"
```

```sh
# Rename .txt files in a folder (depth 1)
lf tree work ~/docs -m _script -f _test

# Show tree only
lf tree show ~/docs -d 2 -s
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

## 🚩 cheque

Render whole-dollar amounts as formal wording for Hong Kong cheques in both Traditional Chinese and English.

Usage:
```sh
# From the repo
python cli.py cheque AMOUNT
```

- `AMOUNT`: Non-negative integer, in Hong Kong dollars (no cents). Examples: `0`, `10`, `1234567`.

Output format:
- Chinese: `中文：港幣<FINANCIAL_UPPERCASE>元正`
- English: `English: Hong Kong Dollars <words> only`

Rules implemented:
- Chinese uses financial uppercase numerals (壹貳叁肆伍陸柒捌玖零) with units: 仟佰拾 within each group and 萬/億/兆 across groups.
- Inserts a single `零` where a unit gap is present (e.g., 1001 -> 壹仟零壹；1000001 -> 壹佰萬零壹)。
- English follows British/HK style:
	- Uses "and" within hundreds (e.g., one hundred and two).
	- Uses "and" between the last group (<100) and a higher group (e.g., one thousand and ten).
	- Hyphenates 21–99 (e.g., twenty-one).

Examples:
```text
python cli.py cheque 0
中文：港幣零元正
English: Hong Kong Dollars Zero only

python cli.py cheque 1001
中文：港幣壹仟零壹元正
English: Hong Kong Dollars One thousand and one only

python cli.py cheque 1000001
中文：港幣壹佰萬零壹元正
English: Hong Kong Dollars One million and one only

python cli.py cheque 120034
中文：港幣壹拾貳萬零叁拾肆元正
English: Hong Kong Dollars One hundred and twenty thousand and thirty-four only
```

Notes:
- Extend English scales beyond trillion by editing `tools/cheque.py` if needed.
- If cents are required in the future, the helpers can be expanded to include sub-dollar parts.

---

## 🚩 word

Plot CSV columns in a PyQt6/pyqtgraph window. The first column is used as the X-axis if it is time-like (timestamps or parseable datetimes) or numeric index-like; otherwise, row index is used. Every other column that’s mostly numeric becomes a subplot.

**Usage:**
```sh
# From the repo
python cli.py word plot FILE [--delimiter DELIM] [--title TITLE]

# Installed entrypoint
lf word plot FILE [--delimiter DELIM] [--title TITLE]
```

Options:
- `FILE` (required): CSV/TSV file path.
- `-d, --delimiter`: CSV delimiter. If omitted, it’s auto-detected (commas, tabs, semicolons, pipes, space).
- `-t, --title`: Window title.

Behavior:
- X-axis detection:
	- Time-like: ISO-ish strings or values parseable by python-dateutil (if installed); numeric epochs in seconds (>1e9) or milliseconds (>1e12).
	- Index-like: numeric first column.
	- Otherwise: row index 0..N-1.
- Y-columns: columns with at least ~60% numeric values are treated as “data-like” and each gets its own subplot. Non-numeric/missing values become gaps.
- Time X-axis uses pyqtgraph’s `DateAxisItem`.

Examples:
```sh
lf word plot ~/data/sensors.csv
lf word plot ~/data/log.tsv -d $'\t' -t "Device Log"
```

Requirements:
- GUI environment (macOS, Windows, or X11/Wayland Linux desktop).
- Python packages are installed automatically via requirements: `PyQt6`, `pyqtgraph`. For broader date parsing, you can `pip install python-dateutil` (optional).

---

## Project Structure

```
cli.py               # Main CLI entry point
tools/
  tree.py            # Directory tree tool
  youtube.py         # YouTube downloader tool
  clock.py           # Full-screen seven-segment terminal clock
	cheque.py          # HK cheque wording (Chinese + English)
	word.py            # CSV plotting with pyqtgraph (PyQt6)
```

## License

See `LICENSE` for details.
