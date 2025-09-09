
# toolbox

A set of useful command-line tools for enhancing productivity.

## Features

- **tree**: Display a directory tree with optional depth/hidden skipping, plus a flexible batch file processor that imports your function from a sibling Python file.
- **youtube**: Download YouTube videos, audio, and subtitles, or display video metadata.
- **clock**: Full-screen seven-segment terminal clock with stopwatch and countdown.
- **cheque**: Convert integer amounts to formal HK cheque wording in Traditional Chinese and English.

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
lf tree process PATH [-m MODULE] [-f FUNC] [-r]

# From the repo
python cli.py tree process PATH [-m MODULE] [-f FUNC] [-r]
```

Options:
- `PATH` (required): A file or a directory. If a directory, all files in it are processed; with `-r/--recursive`, subdirectories are included.
- `-m, --module` (default: `script`): Python module name (without `.py`) in the same folder as `PATH` to import.
- `-f, --func` (default: `test`): Function name to call inside the module. It should accept a single `filepath: str` argument. If the function returns a non-None value, it will be printed.
- `-r, --recursive`: Recurse into subdirectories when `PATH` is a directory.

Behavior niceties:
- If the module file doesnt exist, a template is copied from `tools/script.py` to help you start quickly.
- If the function doesnt exist in the module, a stub will be appended automatically so you can fill it in.

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
lf tree process ~/docs -m script -f test

# Recursive processing
lf tree process ~/docs -m script -f test -r

# Single file
lf tree process ~/docs/notes.txt -m script -f test
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

## Project Structure

```
cli.py               # Main CLI entry point
tools/
  tree.py            # Directory tree tool
  youtube.py         # YouTube downloader tool
  clock.py           # Full-screen seven-segment terminal clock
	cheque.py          # HK cheque wording (Chinese + English)
```

## License

See `LICENSE` for details.
