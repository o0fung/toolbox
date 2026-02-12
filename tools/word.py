import csv
import math
import os
import ssl
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

import typer
from rich import print


try:
    # Optional but highly recommended for robust datetime parsing
    import dateutil.parser as dateparser  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    dateparser = None  # fallback to fromisoformat-only


# User can access help message with shortcut -h
app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})

CLOUDCONVERT_MARKDOWN_SYNTAX = "github"
CLOUDCONVERT_API_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiIxIiwianRpIjoiODc2NjRkY2Q3YWI5NGMwMzgwNTM4YjUzMWEyMTE5NGZiMDA4NTllNmQzMWIyZTc4ZjAwZjk4OTY0OTAwOWE5M2FlZjQyMmM4MTg2NmQyOTAiLCJpYXQiOjE3NzA4Nzc1MzIuOTc4OTA2LCJuYmYiOjE3NzA4Nzc1MzIuOTc4OTA3LCJleHAiOjQ5MjY1NTExMzIuOTcxOTMzLCJzdWIiOiIzNDcyODkyMyIsInNjb3BlcyI6WyJ1c2VyLnJlYWQiLCJ1c2VyLndyaXRlIiwidGFzay5yZWFkIiwidGFzay53cml0ZSIsIndlYmhvb2sucmVhZCIsIndlYmhvb2sud3JpdGUiLCJwcmVzZXQucmVhZCIsInByZXNldC53cml0ZSJdfQ.XlSBOAL1fo7UIOjvfq-I3PxzYlS-53vMPmhuzDWWBrrQchJ0sozqKTS5rHlmV0_mUy0XFTe01Yhs1eZ_-fCMGanaG5X1EekXfBOOOjMBfmy1UWxS4_lQ5OS8eI-rdMHla_Np35OyRiFl9Y-CFHrg0GjT-UyCUrBFndkzId-3WOhOK-hImCLVarlCGtHTinrNImbJ5Yh6rPQbzBAerVtZ-EZm0ivk8F3GLNlW6dSdhGEOTosnk04SmXVsFiXqMK-nGGc3OCm6pBrD5Dw4MJjujZpm1PVlWbyNnKOPsrqnGPkeAFiZdi7Bpvtuaxqt8bCzQuFgi07jVL9l90L35slttnSwHmGhTO3VuYeNjN-hRYfMB8dr1eq0sxRJlYYmi-EYTPHcEDlIIuI2yvWqJoGC8SMk92lJD8f62ettYlx89k6eJ6goJKpbukc9SCtXscUeVGsde-DBMBrlH4ApxERBJMjXs03meB682b-NcMKz9Qn-Qtyrbkjvu4s19LhHsTX6kfrnk6MB2bKUc8c1Tlu6ucTnTCa0W9Y9Ve9I0MlMWRQUiHfsaPBbCOTM5mjeA8cHEv4OYn62Fhem-q-lIADpwrZHucFSe2-Rn4jC_sVQ8Q6RDEGb9Cj-KA0Xi4rzjk-0bi_wmVZ50APrYzxueqHByFHA-Q22OkjQQrVOO3_DQ8o"


class CloudConvertError(RuntimeError):
    """Raised for CloudConvert conversion failures."""


def _step(message: str, enabled: bool = True) -> None:
    if enabled:
        print(f"[bold cyan]md[/bold cyan] {message}")


def _mask_key(key: str) -> str:
    if len(key) < 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


@dataclass
class ParsedCSV:
    headers: List[str]
    rows: List[List[str]]


def _sniff_delimiter(sample: str, fallback: str = ",") -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;| ")
        return dialect.delimiter
    except Exception:
        return fallback


def _read_csv(path: str, delimiter: Optional[str]) -> ParsedCSV:
    """Read CSV file with optional delimiter auto-sniff and header detection."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise typer.BadParameter(f"CSV not found: {path}")

    # Read a small sample to sniff delimiter
    with open(path, "r", encoding="utf-8", newline="") as f:
        sample = f.read(4096)
    delim = delimiter or _sniff_delimiter(sample)

    # Now parse fully
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=delim)
        all_rows: List[List[str]] = [list(r) for r in reader]

    if not all_rows:
        raise typer.BadParameter("CSV is empty")

    first = all_rows[0]
    # Heuristic header detection: if any cell contains alphabetic characters
    def has_alpha(s: str) -> bool:
        return any(ch.isalpha() for ch in s)

    header_present = any(has_alpha(cell or "") for cell in first)
    if header_present:
        headers = [h.strip() or f"col{i}" for i, h in enumerate(first)]
        rows = all_rows[1:]
    else:
        headers = [f"col{i}" for i in range(len(first))]
        rows = all_rows

    return ParsedCSV(headers=headers, rows=rows)


def _to_float_list(values: List[str]) -> Tuple[List[float], int]:
    ok = 0
    out: List[float] = []
    for v in values:
        try:
            if v is None or v == "":
                out.append(math.nan)
            else:
                out.append(float(v))
                ok += 1
        except Exception:
            out.append(math.nan)
    return out, ok


def _try_parse_datetime(s: str) -> Optional[datetime]:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    # If numeric string, treat as unix timestamp seconds or ms if large
    try:
        x = float(s)
        # Heuristic: treat > 1e12 as ms, > 1e9 as seconds
        if x > 1e12:
            return datetime.fromtimestamp(x / 1000.0)
        if x > 1e9:
            return datetime.fromtimestamp(x)
        # Otherwise it's likely an index-like number; don't treat as datetime
        return None
    except Exception:
        pass

    # ISO-like
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass

    if dateparser is not None:
        try:
            return dateparser.parse(s)
        except Exception:
            return None
    return None


def _column_as_x(rows: List[List[str]], col_index: int) -> Tuple[str, List[float], str]:
    """Decide x-axis nature from an arbitrary column index.

    Returns (x_kind, x_values, x_label) where x_kind in {"time", "index", "row"}.
    - time: epoch seconds list (float) suitable for DateAxisItem
    - index: numeric values
    - row: fallback row indices 0..N-1 (column unsuitable)
    """
    if not rows:
        return "row", [], "row"

    col_vals = [r[col_index] if len(r) > col_index else "" for r in rows]

    parsed_dt: List[Optional[datetime]] = [_try_parse_datetime(s) for s in col_vals]
    dt_ok = sum(1 for d in parsed_dt if d is not None)
    if dt_ok >= max(3, int(0.8 * len(col_vals))):
        xs = [d.timestamp() if d is not None else math.nan for d in parsed_dt]
        return "time", xs, "time"

    xs_num, ok = _to_float_list(col_vals)
    if ok >= max(3, int(0.8 * len(col_vals))):
        return "index", xs_num, "index"

    return "row", list(range(len(rows))), "row"


def _collect_numeric_columns(headers: List[str], rows: List[List[str]], skip_indices: Iterable[int]) -> List[Tuple[int, str, List[float]]]:
    cols: List[Tuple[int, str, List[float]]] = []
    ncols = max(len(r) for r in rows) if rows else 0
    skips = set(skip_indices)
    for j in range(ncols):
        if j in skips:
            continue
        col_vals = [r[j] if len(r) > j else "" for r in rows]
        floats, ok = _to_float_list(col_vals)
        if ok >= max(2, int(0.6 * len(col_vals))):  # threshold heuristic
            name = headers[j] if j < len(headers) else f"col{j}"
            cols.append((j, name, floats))
    return cols


def _resolve_col_token(token: str, headers: List[str]) -> Optional[int]:
    token = token.strip()
    if token == "":
        return None
    if token.isdigit():
        return int(token)
    # Case-insensitive exact match
    lowered = [h.lower() for h in headers]
    try:
        return lowered.index(token.lower())
    except ValueError:
        return None


def _require_cloudconvert_api_key() -> str:
    key = (CLOUDCONVERT_API_KEY or "").strip()
    if key:
        return key
    raise typer.BadParameter(
        "Missing CloudConvert API key constant: CLOUDCONVERT_API_KEY."
    )


def _load_cloudconvert_sdk():
    try:
        import cloudconvert  # type: ignore
    except Exception as exc:
        raise CloudConvertError(
            "The 'cloudconvert' package is required. Install it with: pip install cloudconvert"
        ) from exc
    return cloudconvert


def _get_job_task_id(tasks: List[Dict[str, Any]], task_name: str) -> str:
    for task in tasks:
        if task.get("name") != task_name:
            continue
        task_id = str(task.get("id", "")).strip()
        if task_id:
            return task_id
    raise CloudConvertError(f"CloudConvert did not return task '{task_name}'.")


def _download_file_with_certifi_tls(download_url: str, output_path: Path, verbose: bool) -> None:
    _step("Step 8/9: downloading output DOCX with certifi TLS bundle", verbose)

    try:
        import certifi  # type: ignore
    except Exception as exc:
        raise CloudConvertError(
            "The 'certifi' package is required for secure download verification."
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ok = False

    try:
        req = urlrequest.Request(download_url, method="GET")
        with urlrequest.urlopen(req, timeout=300, context=ssl_context) as response, temp_path.open(
            "wb"
        ) as target:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                target.write(chunk)
        ok = True
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        raise CloudConvertError(
            f"Download HTTP error ({exc.code}): {body or 'no response body'}"
        ) from exc
    except urlerror.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise CloudConvertError(
            "Download URL/TLS error. "
            f"Reason: {reason}. certifi bundle: {certifi.where()}"
        ) from exc
    except Exception as exc:
        raise CloudConvertError(f"Download failed: {exc}") from exc
    finally:
        if (not ok) and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass

    temp_path.replace(output_path)


def _convert_markdown_to_docx_cloudconvert(input_path: Path, api_key: str, verbose: bool = True) -> Path:
    _step("Step 1/9: loading cloudconvert SDK", verbose)
    cloudconvert = _load_cloudconvert_sdk()

    _step("Step 2/9: configuring cloudconvert client", verbose)
    cloudconvert.configure(api_key=api_key, sandbox=False)

    output_path = input_path.with_suffix(".docx")
    payload: Dict[str, Any] = {
        "tasks": {
            "import-md": {
                "operation": "import/upload",
            },
            "convert-md": {
                "operation": "convert",
                "input": "import-md",
                "input_format": "md",
                "output_format": "docx",
                "input_markdown_syntax": CLOUDCONVERT_MARKDOWN_SYNTAX,
            },
            "export-docx": {
                "operation": "export/url",
                "input": "convert-md",
            },
        }
    }

    _step(
        "Step 3/9: creating CloudConvert job (import/upload -> convert md->docx -> export/url)",
        verbose,
    )
    try:
        job = cloudconvert.Job.create(payload=payload)
    except Exception as exc:
        raise CloudConvertError(f"Failed to create CloudConvert job: {exc}") from exc

    job_id = str(job.get("id", "")).strip()
    if job_id:
        _step(f"Created job id: {job_id}", verbose)

    tasks = job.get("tasks", [])
    if not isinstance(tasks, list):
        raise CloudConvertError("CloudConvert returned an invalid tasks list.")

    _step("Step 4/9: resolving job task ids", verbose)
    import_task_id = _get_job_task_id(tasks, "import-md")
    export_task_id = _get_job_task_id(tasks, "export-docx")
    _step(f"import task id: {import_task_id}", verbose)
    _step(f"export task id: {export_task_id}", verbose)

    _step("Step 5/9: fetching upload task details", verbose)
    try:
        import_task = cloudconvert.Task.find(id=import_task_id)
    except Exception as exc:
        raise CloudConvertError(f"Failed to fetch upload task: {exc}") from exc

    _step(f"Step 6/9: uploading markdown file ({input_path.name})", verbose)
    try:
        uploaded = cloudconvert.Task.upload(file_name=str(input_path), task=import_task)
    except Exception as exc:
        raise CloudConvertError(f"CloudConvert upload failed: {exc}") from exc
    if not uploaded:
        raise CloudConvertError("CloudConvert upload failed.")

    _step("Step 7/9: waiting for conversion/export task to finish", verbose)
    try:
        export_task = cloudconvert.Task.wait(id=export_task_id)
    except Exception as exc:
        raise CloudConvertError(f"CloudConvert conversion failed: {exc}") from exc

    status = str(export_task.get("status", "")).strip()
    if status and status != "finished":
        message = str(export_task.get("message") or "").strip()
        if message:
            raise CloudConvertError(
                f"CloudConvert export task ended with status '{status}': {message}"
            )
        raise CloudConvertError(f"CloudConvert export task ended with status '{status}'.")

    files = export_task.get("result", {}).get("files", [])
    if not isinstance(files, list) or not files:
        raise CloudConvertError("CloudConvert did not return any output file.")

    file_name = str(files[0].get("filename", "")).strip()
    download_url = str(files[0].get("url", "")).strip()
    if not download_url:
        raise CloudConvertError("CloudConvert did not return an export URL.")
    _step(f"CloudConvert output file: {file_name or '(unknown filename)'}", verbose)
    _step(f"Download host: {urlparse(download_url).netloc}", verbose)

    _download_file_with_certifi_tls(download_url, output_path, verbose)
    _step("Step 9/9: conversion finished", verbose)
    return output_path


@app.command("md")
def md(
    md_path: str = typer.Argument(..., help="Path to Markdown (.md) file to convert"),
    verbose: bool = typer.Option(
        True,
        "--verbose/--quiet",
        help="Show detailed step-by-step progress for CloudConvert conversion.",
    ),
):
    """Convert one Markdown file to DOCX using CloudConvert."""
    _step("Step 0/9: validating input file path", verbose)
    input_path = Path(os.path.expanduser(md_path))
    if input_path.suffix.lower() != ".md":
        raise typer.BadParameter(f"Input file must use a .md extension: {input_path}")
    if not input_path.is_file():
        raise typer.BadParameter(f"Markdown file not found: {input_path}")

    _step("Resolving CloudConvert API key from constant", verbose)
    resolved_key = _require_cloudconvert_api_key()
    _step(f"Using API key: {_mask_key(resolved_key)}", verbose)
    print(f"Converting markdown to DOCX: {input_path}")
    try:
        output_path = _convert_markdown_to_docx_cloudconvert(input_path, resolved_key, verbose=verbose)
    except CloudConvertError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    print(f"[green]Created DOCX[/green]: {output_path}")


# CSV plotting command
@app.command("plot")
def plot(
    csv_path: str = typer.Argument(..., help="Path to CSV file"),
    delimiter: Optional[str] = typer.Option(None, "-d", "--delimiter", help="CSV delimiter (auto if omitted)"),
    title: Optional[str] = typer.Option(None, "-t", "--title", help="Window title"),
    save: bool = typer.Option(False, "-s", "--save", help="Export a high-resolution PNG (ImageExporter) and exit."),
    out_path: Optional[str] = typer.Option(
        None,
        "-o",
        "--out-path",
        help="Output PNG file path or directory when using --save. If a directory or ends with a path separator, the file name <csv_basename>.png is used. Extension .png will be appended if missing. Providing this flag implies --save if not explicitly set.",
    ),
    xcol: Optional[str] = typer.Option(None, "-x", "--xcol", help="Column (name or index) to use as X axis (time/index). Default: auto from first column"),
    ycols: Optional[str] = typer.Option(None, "-y", "--ycols", help="Comma-separated columns (names or indices) for Y subplots. Default: all numeric except xcol"),
    xlim: Optional[str] = typer.Option(None, "--xlim", help="Row index range start,end inclusive (e.g. 200,300)."),
    weight: float = typer.Option(1.0, "-w", "--weight", help="Line width (pixels) for plotted lines (e.g. 1, 1.5, 2)."),
):
    """Plot CSV columns using pyqtgraph with subplots per data column.

    Rules:
    - First column used as x if time-like or numeric index-like; else use row index.
    - Every other column that's mostly numeric becomes a subplot.
    - Time x-axis uses a DateAxisItem.
    """
    data = _read_csv(csv_path, delimiter)
    ncols = max(len(r) for r in data.rows) if data.rows else 0

    # Resolve x column index
    if xcol is None:
        x_index = 0 if ncols > 0 else -1
    else:
        x_index = _resolve_col_token(xcol, data.headers)
        if x_index is None or x_index < 0 or x_index >= ncols:
            raise typer.BadParameter(f"--xcol '{xcol}' not found")

    x_kind, xs, _ = _column_as_x(data.rows, x_index) if x_index >= 0 else ("row", [], "row")
    total_original_points = len(xs)

    # Determine Y column indices
    if ycols:
        chosen_indices: List[int] = []
        for tok in ycols.split(','):
            idx = _resolve_col_token(tok, data.headers)
            if idx is None:
                print(f"[yellow]Warning[/yellow]: y column token '{tok.strip()}' not found; skipping")
                continue
            if idx == x_index:
                print(f"[yellow]Warning[/yellow]: y column token '{tok.strip()}' is xcol; skipping")
                continue
            if idx < 0 or idx >= ncols:
                print(f"[yellow]Warning[/yellow]: y column index {idx} out of range; skipping")
                continue
            if idx not in chosen_indices:
                chosen_indices.append(idx)
        # Build y list (include even if low numeric content but warn)
        ycols_list: List[Tuple[int, str, List[float]]] = []
        for idx in chosen_indices:
            vals = [r[idx] if len(r) > idx else "" for r in data.rows]
            floats, ok = _to_float_list(vals)
            name = data.headers[idx] if idx < len(data.headers) else f"col{idx}"
            if ok == 0:
                print(f"[yellow]Warning[/yellow]: column '{name}' has no numeric data; will appear empty")
            ycols_list.append((idx, name, floats))
    else:
        # Auto collect numeric except x
        ycols_list = _collect_numeric_columns(data.headers, data.rows, skip_indices=[x_index])

    if not ycols_list:
        raise typer.BadParameter("No Y columns to plot")

    print(f"[bold cyan]Loaded[/bold cyan] {len(data.rows)} rows, {len(data.headers)} columns from: {csv_path}")
    x_name = data.headers[x_index] if (0 <= x_index < len(data.headers)) else "row"
    if 0 <= x_index < len(data.headers):
        print(f"Using x-axis: {x_kind} (column: {x_name}[{x_index}])")
    else:
        print(f"Using x-axis: {x_kind} (column: row index)")
    # Derive available channel counts
    total_cols = len(data.headers)
    available_y_total = max(0, total_cols - (1 if x_index >= 0 else 0))
    selected_y = len(ycols_list)
    # Also compute how many of the available columns are numeric-eligible (auto detection) for context
    auto_numeric_candidates = _collect_numeric_columns(data.headers, data.rows, skip_indices=[x_index])
    numeric_candidate_count = len(auto_numeric_candidates)
    # Detailed channel lists with indices
    selected_pairs = [(idx, name) for idx, name, _ in ycols_list]
    selected_display = [f"{name}[{idx}]" for idx, name in selected_pairs]
    # Build full candidate list (excluding x)
    all_candidate_indices = [i for i in range(total_cols) if i != x_index]
    selected_indices_set = {idx for idx, _ in selected_pairs}
    unselected_pairs = [(i, data.headers[i]) for i in all_candidate_indices if i not in selected_indices_set]
    unselected_display = [f"{name}[{i}]" for i, name in unselected_pairs]
    print(f"Y subplots: ({len(selected_pairs)} / {len(all_candidate_indices)})")
    print(
        f"Selected channels ({len(selected_display)}): " + (', '.join(selected_display) if selected_display else '(none)')
    )
    print(
        f"Unselected channels ({len(unselected_display)}): " + (', '.join(unselected_display) if unselected_display else '(none)')
    )

    # Range-based index trimming only
    total_points = len(xs)
    if xlim is not None:
        raw = xlim.replace(' ', '')
        sep = ',' if ',' in raw else (':' if ':' in raw else None)
        if sep is None:
            raise typer.BadParameter("--range must be in form start,end (comma or colon)")
        parts = raw.split(sep)
        if len(parts) != 2:
            raise typer.BadParameter("--range expects exactly two numbers: start,end")
        try:
            imin = int(parts[0]) if parts[0] != '' else 0
            imax = int(parts[1]) if parts[1] != '' else total_points - 1
        except ValueError:
            raise typer.BadParameter("--range values must be integers")
        if imin < 0:
            imin = 0
        if imax >= total_points:
            imax = total_points - 1
        if imin > imax:
            raise typer.BadParameter("Index trim invalid: start > end")
        r_idx = range(imin, imax + 1)
        xs = [xs[i] for i in r_idx]
        trimmed_y: List[Tuple[int, str, List[float]]] = []
        for idx, name, arr in ycols_list:
            trimmed_y.append((idx, name, [arr[i] if i < len(arr) else math.nan for i in r_idx]))
        ycols_list = trimmed_y

    # Report final number of x-axis data points (after any trimming)
    if len(xs) == total_original_points:
        print(f"Data points (x): {len(xs)} (full dataset)")
    else:
        print(f"Data points (x): [{imin},{imax}] ({len(xs)} / {total_original_points} points selected)")

    # Lazy import Qt/pyqtgraph only when plotting
    try:
        from PyQt6 import QtWidgets
        import pyqtgraph as pg
        from pyqtgraph import DateAxisItem
    except Exception as e:
        raise typer.BadParameter(
            "PyQt6/pyqtgraph not available. Please install with: pip install PyQt6 pyqtgraph"
        ) from e

    app_qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    # Apply white theme BEFORE creating widgets to override defaults / dark mode
    pg.setConfigOption('background', (255, 255, 255))
    pg.setConfigOption('foreground', (0, 0, 0))

    win = pg.GraphicsLayoutWidget()
    win.setWindowTitle(title or f"CSV Plot - {os.path.basename(csv_path)}")
    win.resize(1000, 800)

    # Extra safety: enforce palette on window (in some dark system themes)
    try:
        pal = win.palette()
        pal.setColor(pal.ColorRole.Window, QtWidgets.QColor(255, 255, 255))
        win.setPalette(pal)
    except Exception:
        pass

    # Tighten layout spacing (horizontal, vertical)
    try:
        win.ci.setSpacing(8, 3)
    except Exception:
        pass

    pen_colors = [
        (33, 150, 243),   # blue
        (244, 67, 54),    # red
        (76, 175, 80),    # green
        (255, 152, 0),    # orange
        (156, 39, 176),   # purple
        (0, 121, 107),    # teal
    ]

    # Validate line weight
    if not (weight > 0):  # allow float > 0
        raise typer.BadParameter("--weight must be > 0")

    first_plot = None
    nplots = len(ycols_list)
    for i, (_idx, name, ys) in enumerate(ycols_list):
        is_last = (i == nplots - 1)
        if x_kind == "time" and is_last:
            axis_items = {"bottom": DateAxisItem(orientation="bottom")}
        else:
            axis_items = None
        p = win.addPlot(row=i, col=0, axisItems=axis_items)
        p.setLabel("left", name)
        p.showGrid(x=True, y=True, alpha=0.12)
        try:
            p.getAxis('left').setPen('k'); p.getAxis('left').setTextPen('k')
            p.getAxis('bottom').setPen('k'); p.getAxis('bottom').setTextPen('k')
        except Exception:
            pass
        if not is_last:
            try:
                p.getAxis('bottom').setStyle(showValues=False)
                if hasattr(p.getAxis('bottom'), 'setHeight'):
                    p.getAxis('bottom').setHeight(2)
            except Exception:
                pass
        else:
            if x_kind == "time":
                p.setLabel('bottom', 'Time')
            elif x_kind == "index":
                p.setLabel('bottom', x_name)
            else:
                p.setLabel('bottom', 'Index')
        if first_plot is not None:
            p.setXLink(first_plot)
        color = pen_colors[i % len(pen_colors)]
        p.plot(xs, ys, pen=pg.mkPen(color=color, width=weight))
        if first_plot is None:
            first_plot = p


    # Ensure layouts are finalized before exporting or showing
    app_qt.processEvents()

    # If user supplied an out_path but did not pass --save, assume they intended to save.
    if (out_path is not None) and (not save):
        print("[yellow]Note[/yellow]: --out-path provided without --save; enabling save mode.")
        save = True  # type: ignore

    if save:
        # Always perform high-resolution export using ImageExporter
        win.show()
        app_qt.processEvents()
        base, _ = os.path.splitext(os.path.expanduser(csv_path))

        # Resolve output path behavior:
        # 1. If user provided out_path treat it as either a directory or file path.
        # 2. If directory (exists or endswith path sep) -> join with <csv_basename>.png
        # 3. If extension missing add .png
        # 4. If not provided default to <csv_file_basename>.png next to CSV.
        if out_path is None:
            resolved_out = base + ".png"
        else:
            candidate = os.path.expanduser(out_path)
            # If ends with separator or is an existing directory treat as directory
            if candidate.endswith(os.sep) or os.path.isdir(candidate):
                os.makedirs(candidate, exist_ok=True)
                resolved_out = os.path.join(candidate, os.path.basename(base) + ".png")
            else:
                parent = os.path.dirname(candidate) or "."
                if not os.path.isdir(parent):
                    try:
                        os.makedirs(parent, exist_ok=True)
                    except Exception as e:
                        raise typer.BadParameter(f"Cannot create parent directory for out-path: {parent}: {e}") from e
                root, ext = os.path.splitext(candidate)
                if ext.lower() != ".png":
                    resolved_out = candidate + ".png"
                else:
                    resolved_out = candidate
        out_path = resolved_out
        try:
            from pyqtgraph.exporters import ImageExporter  # type: ignore
        except Exception as e:
            print("[red]Export requires pyqtgraph.exporters (ensure pyqtgraph is up to date).[/red]")
            raise typer.Exit(code=1) from e

        target_item = getattr(win, 'ci', None) or first_plot
        exporter = ImageExporter(target_item)
        width_env = os.environ.get('WORD_EXPORT_WIDTH', '2400')
        per_plot_env = os.environ.get('WORD_EXPORT_PER_PLOT', '210')
        try:
            width_px = int(width_env)
            per_plot_height = int(per_plot_env)
        except ValueError:
            print("[yellow]Warning:[/yellow] WORD_EXPORT_WIDTH or WORD_EXPORT_PER_PLOT invalid; using defaults (2400, 210)")
            width_px = 2400
            per_plot_height = 210
        exporter.parameters()['width'] = width_px
        exporter.parameters()['height'] = max(600, per_plot_height * nplots)
        try:
            exporter.export(out_path)
            print(f"[green]Saved PNG[/green]: {out_path} (width={width_px}px, plots={nplots}, line-width={weight})")
        except Exception as e:
            print(f"[red]Failed export[/red]: {e}")
            raise typer.Exit(code=1) from e
        return

    # Interactive mode
    # Add an ESC key shortcut so user can quickly close the window
    try:  # Guard in case QtGui not available for some reason
        from PyQt6 import QtGui, QtCore  # type: ignore
        esc_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Escape"), win)
        esc_shortcut.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        esc_shortcut.activated.connect(win.close)  # type: ignore[arg-type]
    except Exception:
        pass

    win.show()
    print("[dim]Press ESC to close the window.[/dim]")
    app_qt.exec()


# Entry point for running the script directly
if __name__ == '__main__':
    app()
