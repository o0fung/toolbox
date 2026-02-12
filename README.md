
# toolbox

A set of useful command-line tools for enhancing productivity.

## Features

- **tree**: Display a directory tree with optional depth/hidden skipping, plus a flexible batch file processor that imports your function from a sibling Python file.
- **youtube**: Download YouTube videos, audio, and subtitles, or display video metadata.
- **clock**: Full-screen seven-segment terminal clock with stopwatch and countdown.
- **cheque**: Convert HKD amounts (supports cents) to formal HK cheque wording in Traditional Chinese and English.
- **word**: Convert Markdown to DOCX (CloudConvert) and plot CSV data with pyqtgraph subplots.

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

Download YouTube content, list formats, or show metadata (powered by `yt-dlp`).

**Basic Usage:**
```sh
python cli.py youtube URL [options]
```

If no download-related flags are given, metadata only is displayed.

**Common Flags:**
- `URL` (positional): YouTube video URL.
- `--video`, `-v`: Download best video (mp4-preferred) with audio using smart fallback formats.
- `--audio`, `-a`: Download best audio and convert to mp3 (192 kbps) via ffmpeg.
- `--subtitle`, `-s`: Download English subtitles (manual + auto). Use with other modes or alone (metadata + subs).
- `--list`: List all available formats (no download) in a compact table.
- `--fmt FORMAT_EXPR`: Explicit yt-dlp format selector (e.g. `251`, `137+251`, `bestvideo[height<=720]+bestaudio/best`). Overrides `-v/-a` logic.
- `--out DIR`: Output directory (default: `~/Desktop`). Created if missing.

**Format Fallback Logic (when using `-v` without `--fmt`):**
1. `bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]`
2. `bestvideo*+bestaudio*/bestvideo+bestaudio`
3. `best`

Each failed attempt logs a warning and moves to the next expression. On total failure you'll be prompted to run `--list` and choose a format via `--fmt`.

**Examples:**
```sh
# Show metadata only
python cli.py youtube https://www.youtube.com/watch?v=ID

# List formats
python cli.py youtube https://www.youtube.com/watch?v=ID --list

# Download best mp4 (auto fallback) to Desktop
python cli.py youtube https://www.youtube.com/watch?v=ID -v

# Download audio only as mp3 to a custom folder
python cli.py youtube https://www.youtube.com/watch?v=ID -a --out ~/Media/audio

# Download subtitles only (no media)
python cli.py youtube https://www.youtube.com/watch?v=ID -s

# Explicit format combine 1080p video (137) + opus audio (251)
python cli.py youtube https://www.youtube.com/watch?v=ID --fmt 137+251 --out ./downloads

# Constrain height (yt-dlp expression)
python cli.py youtube https://www.youtube.com/watch?v=ID --fmt "bestvideo[height<=720]+bestaudio/best[height<=720]"
```

**Notes:**
- Filenames are the video title; adjust or sanitize as needed manually for now.
- Subtitles default language: English (`--subtitle` enables both manual + auto if available).
- Use `--fmt` for full control; all yt-dlp format selectors are supported.
- Output directory is echoed at start: `[info] Output directory: /path/...`
- When all format attempts fail you will see a hint to run `--list`.

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

Render HKD amounts (dollars + optional cents) as formal wording for Hong Kong cheques in both Traditional Chinese and English.

Usage:
```sh
# From the repo
python cli.py cheque AMOUNT
```

- `AMOUNT`: Non-negative numeric amount with up to 2 decimal places. Examples: `0`, `10`, `1234567`, `123.45`, `0.05`.

Output format:
- Chinese:
	- cents is zero: `中文：港幣<FINANCIAL_UPPERCASE>元正`
	- cents exists: `中文：港幣<FINANCIAL_UPPERCASE>元<角分>`
- English: `English: Hong Kong Dollars <words> only`

Rules implemented:
- Chinese uses financial uppercase numerals (壹貳叁肆伍陸柒捌玖零) with units: 仟佰拾 within each group and 萬/億/兆 across groups.
- Inserts a single `零` where a unit gap is present (e.g., 1001 -> 壹仟零壹；1000001 -> 壹佰萬零壹)。
- Chinese cents are rendered with `角`/`分` (e.g., `0.05` -> `零伍分`).
- English follows British/HK style:
	- Uses "and" within hundreds (e.g., one hundred and two).
	- Uses "and" between the last group (<100) and a higher group (e.g., one thousand and ten).
	- Hyphenates 21–99 (e.g., twenty-one).
	- Cents are rendered in words (e.g., `123.45` -> `... and forty-five cents only`).

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

python cli.py cheque 123.45
中文：港幣壹佰貳拾叁元肆角伍分
English: Hong Kong Dollars One hundred and twenty-three and forty-five cents only

python cli.py cheque 0.05
中文：港幣零元零伍分
English: Hong Kong Dollars Zero and five cents only
```

Notes:
- Extend English scales beyond trillion by editing `tools/cheque.py` if needed.

---

## 🚩 word

`word` contains two subcommands:
- `md`: convert one Markdown file to DOCX via CloudConvert.
- `plot`: plot CSV columns in stacked subplots using PyQt6 + pyqtgraph.

### Markdown to DOCX (`word md`)

Usage:
```sh
# From the repo
python cli.py word md notes.md --api-key "$CLOUDCONVERT_API_KEY"

# Installed entrypoint
lf word md notes.md --api-key "$CLOUDCONVERT_API_KEY"

# Or rely on environment variable
export CLOUDCONVERT_API_KEY="your_api_key_here"
lf word md notes.md
```

Options:
- `--api-key`: CloudConvert API key. If omitted, `CLOUDCONVERT_API_KEY` env var is used.
- `--verbose/--quiet`: Toggle detailed conversion steps.

### CSV plotting (`word plot`)

**Usage:**
```sh
# From the repo
python cli.py word plot FILE [options]

# Installed entrypoint
lf word plot FILE [options]
```

**Key Options:**
- `FILE` (required): CSV/TSV file path.
- `-d, --delimiter DELIM`: Force delimiter (auto-sniff if omitted across , \t ; | space).
- `-t, --title TEXT`: Window title.
- `-x, --xcol NAME|INDEX`: Column to use as X axis (time-like, numeric, or fallback to row indices). Default: first column.
- `-y, --ycols COLS`: Comma-separated list of Y columns (names or indices). Default: all numeric except the chosen x column.
- `--xlim start,end`: Row index slice (inclusive) before plotting. Accepts comma or colon: `--xlim 200,300` or `--xlim 200:300`. Empty start/end allowed (`,500` or `500,`).
- `-s, --save`: Export a high‑resolution PNG (ImageExporter; independent of window size) next to the CSV (same basename) and exit. Env overrides: `WORD_EXPORT_WIDTH`, `WORD_EXPORT_PER_PLOT`.
- `-w, --weight FLOAT`: Line width (in pixels) for all plotted lines (default 1.0). Increase (e.g. 2 or 3) to make thin signals more visible.
- `-o, --out-path PATH`: Output PNG path or directory (implies `--save` if not explicitly provided). If a directory or ends with a path separator, the file name `<csv_basename>.png` is used. `.png` extension appended if missing.

**Automatic X-axis detection:**
1. If selected x column parses as (mostly) datetimes or epoch seconds/milliseconds -> time axis (DateAxisItem).
2. Else if (mostly) numeric -> numeric index using the column’s values.
3. Else -> simple row index (0..N-1).

**Y column selection:**
- Columns with ≥ ~60% numeric entries qualify automatically (unless overridden with `--ycols`).
- Non-numeric cells become gaps (NaN) in the plot.

**Index trimming (`--xlim`):**
- Applied to row indices, not data values or timestamps.
- Example: `--xlim 1000,2000` keeps only rows 1000–2000 inclusive.
- `--xlim ,500` keeps start through 500; `--xlim 500,` keeps 500 through end.

**White Theme:**
- Background forced to white; axes/text black; subtle grid (alpha 0.15).
- Exported PNG uses the same theme.

**Examples:**
```sh
# Quick auto plot
lf word plot ~/data/sensors.csv

# Tab-delimited with custom title
lf word plot ~/data/log.tsv -d $'\t' -t "Device Log"

# Explicit x column by name and selected y columns
lf word plot data.csv -x timestamp -y temperature,pressure,3

# Restrict to row indices 200..300
lf word plot data.csv --xlim 200,300

# Export a high-res PNG (no GUI)
lf word plot data.csv -y acc_x,acc_y,acc_z -s

# Export to a specific file path (auto-enables save mode)
lf word plot data.csv -o ~/Desktop/plots/session1.png

# Custom export size via environment
WORD_EXPORT_WIDTH=3000 WORD_EXPORT_PER_PLOT=250 lf word plot data.csv -s

# Thicker lines for visibility
lf word plot data.csv -y acc_x,acc_y,acc_z -w 2.5
```

**Console Output Summary:** (example)
```text
Loaded 6000 rows, 18 columns from: data.csv
Using x-axis: index (column: time[0])
Y subplots: (6 / 17)
Selected channels (6): acc_x[3], acc_y[4], acc_z[5], gyr_x[6], gyr_y[7], gyr_z[8]
Unselected channels (11): temp[9], pressure[10], battery[11], state[12], ...
Index trim -> kept indices [1000,1500] (501 points)
Data points (x): [1000,1500] (501 / 6000 points selected)
Saved PNG: data.png (width=2400px, plots=6, line-width=2.5)
```
Notes:
- Channel indices in brackets are zero-based column positions from the original CSV.
- Unselected list excludes the chosen x-axis column.
- When no trimming is applied you will see `(full dataset)` instead of a selected fraction.

**Requirements:**
- Desktop GUI environment (macOS, Windows, Linux with X11/Wayland).
- Dependencies: `PyQt6`, `pyqtgraph` (plus optional `python-dateutil` for richer date parsing).

**Keyboard Shortcuts (interactive mode):**
- `Esc`: Close the window and exit.

**Other Behaviors:**
- Supplying `--out-path` without `--save` prints a note and performs a save (no interactive session).
- Rows with non-numeric Y values render gaps (NaN) in lines instead of aborting.

**Planned (possible future additions):** trimmed-segment CSV export, interactive ROI selection, overlay/legend mode, optional scaled vector/SVG export.

---

## Project Structure

```
cli.py          # Main CLI entry point
tools/
	_cli_common.py # Shared Typer app factory
	_cli_output.py # Shared CLI output helpers
	tree.py       # Directory tree tool
	youtube.py    # YouTube downloader tool
	clock.py      # Full-screen seven-segment terminal clock
	cheque.py     # HK cheque wording (Chinese + English)
	word.py       # Word command group (registers subcommands)
	word_md.py    # Markdown conversion (CloudConvert)
	word_plot.py  # CSV plotting with pyqtgraph (PyQt6)
tests/
	test_cheque.py
	test_tree.py
```

## License

See `LICENSE` for details.
