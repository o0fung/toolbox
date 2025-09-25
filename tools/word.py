import csv
import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import typer
from rich import print


try:
    # Optional but highly recommended for robust datetime parsing
    import dateutil.parser as dateparser  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    dateparser = None  # fallback to fromisoformat-only


# User can access help message with shortcut -h
app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})


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


def _first_col_as_x(rows: List[List[str]]) -> Tuple[str, List[float], str]:
    """
    Decide x-axis from first column if time-like or index-like numeric.
    Returns (x_kind, x_values, x_label) where x_kind in {"time", "index", "row"}.
    - time: epoch seconds list for DateAxisItem
    - index: numeric values from first column
    - row: 0..N-1
    """
    if not rows or not rows[0]:
        return "row", list(range(len(rows))), "row"

    first_col = [r[0] if len(r) > 0 else "" for r in rows]

    # Try datetime
    parsed_dt: List[Optional[datetime]] = [_try_parse_datetime(s) for s in first_col]
    dt_ok = sum(1 for d in parsed_dt if d is not None)
    if dt_ok >= max(3, int(0.8 * len(first_col))):
        xs = [d.timestamp() if d is not None else math.nan for d in parsed_dt]
        return "time", xs, "time"

    # Try numeric index-like
    xs_num, ok = _to_float_list(first_col)
    if ok >= max(3, int(0.8 * len(first_col))):
        return "index", xs_num, "index"

    # Fallback: row index
    return "row", list(range(len(rows))), "row"


def _collect_numeric_columns(headers: List[str], rows: List[List[str]]) -> List[Tuple[str, List[float]]]:
    cols: List[Tuple[str, List[float]]] = []
    ncols = max(len(r) for r in rows) if rows else 0
    for j in range(1, ncols):  # skip first column; used as x if suitable
        col_vals = [r[j] if len(r) > j else "" for r in rows]
        floats, ok = _to_float_list(col_vals)
        # Consider column as data-like if at least 60% numeric
        if ok >= max(2, int(0.6 * len(col_vals))):
            name = headers[j] if j < len(headers) else f"col{j}"
            cols.append((name, floats))
    return cols


# Main CLI entry point
@app.command("plot")
def plot(
    csv_path: str = typer.Argument(..., help="Path to CSV file"),
    delimiter: Optional[str] = typer.Option(None, "-d", "--delimiter", help="CSV delimiter (auto if omitted)"),
    title: Optional[str] = typer.Option(None, "-t", "--title", help="Window title"),
    save: bool = typer.Option(False, "-s", "--save", help="Also save a high-res PNG next to the CSV (same name, .png)"),
):
    """Plot CSV columns using pyqtgraph with subplots per data column.

    Rules:
    - First column used as x if time-like or numeric index-like; else use row index.
    - Every other column that's mostly numeric becomes a subplot.
    - Time x-axis uses a DateAxisItem.
    """
    data = _read_csv(csv_path, delimiter)
    x_kind, xs, x_label = _first_col_as_x(data.rows)
    ycols = _collect_numeric_columns(data.headers, data.rows)

    if not ycols:
        raise typer.BadParameter("No numeric columns to plot (besides the first column)")

    print(f"[bold cyan]Loaded[/bold cyan] {len(data.rows)} rows, {len(data.headers)} columns from: {csv_path}")
    print(f"Using x-axis: {x_kind}")
    print(f"Y subplots: {', '.join(name for name, _ in ycols)}")

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

    win = pg.GraphicsLayoutWidget()
    win.setWindowTitle(title or f"CSV Plot - {os.path.basename(csv_path)}")
    win.resize(1000, 800)

    pen_colors = [
        (0, 170, 255),  # cyan
        (255, 120, 0),  # orange
        (50, 220, 50),  # green
        (200, 100, 255),  # purple
        (240, 240, 0),  # yellow
        (255, 80, 80),  # red
    ]

    first_plot = None
    for i, (name, ys) in enumerate(ycols):
        if x_kind == "time":
            axis = DateAxisItem(orientation="bottom")
            p = win.addPlot(row=i, col=0, axisItems={"bottom": axis})
            p.setLabel("bottom", "Time")
        else:
            p = win.addPlot(row=i, col=0)
            p.setLabel("bottom", "Index" if x_kind != "index" else (data.headers[0] if data.headers else "index"))

        p.setLabel("left", name)
        p.showGrid(x=True, y=True, alpha=0.3)
        if first_plot is not None:
            p.setXLink(first_plot)

        color = pen_colors[i % len(pen_colors)]
        p.plot(xs, ys, pen=pg.mkPen(color=color, width=1))

        if first_plot is None:
            first_plot = p


    # Ensure layouts are finalized before exporting or showing
    app_qt.processEvents()

    if save:
        try:
            from pyqtgraph.exporters import ImageExporter  # type: ignore

            # Export the entire layout at higher resolution
            target_item = getattr(win, "ci", None) or first_plot
            exporter = ImageExporter(target_item)
            exporter.parameters()["width"] = 2400  # ~high-res width, aspect preserved

            base, _ = os.path.splitext(os.path.expanduser(csv_path))
            out_path = base + ".png"
            exporter.export(out_path)
            print(f"[green]Saved PNG[/green]: {out_path}")
        except Exception as e:
            print(f"[red]Failed to export PNG[/red]: {e}")
        return  # do not enter UI loop when saving only

    # Interactive mode
    win.show()
    app_qt.exec()


# Entry point for running the script directly
if __name__ == '__main__':
    app()
