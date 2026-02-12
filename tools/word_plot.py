"""CSV plotting command implementation for `word plot`."""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import typer
from rich import print

try:
    from ._cli_output import warn
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_output import warn

try:
    # Optional but highly recommended for robust datetime parsing.
    import dateutil.parser as dateparser  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    dateparser = None


@dataclass
class ParsedCSV:
    headers: List[str]
    rows: List[List[str]]


def register(app: typer.Typer) -> None:
    """Register `plot` subcommand on a parent Typer app."""
    app.command("plot")(plot)


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

    with open(path, "r", encoding="utf-8", newline="") as handle:
        sample = handle.read(4096)
    delim = delimiter or _sniff_delimiter(sample)

    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=delim)
        all_rows: List[List[str]] = [list(row) for row in reader]

    if not all_rows:
        raise typer.BadParameter("CSV is empty")

    first = all_rows[0]

    def has_alpha(value: str) -> bool:
        return any(ch.isalpha() for ch in value)

    header_present = any(has_alpha(cell or "") for cell in first)
    if header_present:
        headers = [item.strip() or f"col{i}" for i, item in enumerate(first)]
        rows = all_rows[1:]
    else:
        headers = [f"col{i}" for i in range(len(first))]
        rows = all_rows

    return ParsedCSV(headers=headers, rows=rows)


def _to_float_list(values: List[str]) -> Tuple[List[float], int]:
    ok = 0
    out: List[float] = []
    for value in values:
        try:
            if value is None or value == "":
                out.append(math.nan)
            else:
                out.append(float(value))
                ok += 1
        except Exception:
            out.append(math.nan)
    return out, ok


def _try_parse_datetime(value: str) -> Optional[datetime]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    # Numeric timestamps: milliseconds or seconds.
    try:
        numeric = float(text)
        if numeric > 1e12:
            return datetime.fromtimestamp(numeric / 1000.0)
        if numeric > 1e9:
            return datetime.fromtimestamp(numeric)
        return None
    except Exception:
        pass

    try:
        return datetime.fromisoformat(text)
    except Exception:
        pass

    if dateparser is not None:
        try:
            return dateparser.parse(text)
        except Exception:
            return None
    return None


def _column_as_x(rows: List[List[str]], col_index: int) -> Tuple[str, List[float], str]:
    """Resolve x-axis kind and values from a selected column."""
    if not rows:
        return "row", [], "row"

    column_values = [row[col_index] if len(row) > col_index else "" for row in rows]

    parsed_dt: List[Optional[datetime]] = [_try_parse_datetime(item) for item in column_values]
    dt_ok = sum(1 for item in parsed_dt if item is not None)
    if dt_ok >= max(3, int(0.8 * len(column_values))):
        xs = [item.timestamp() if item is not None else math.nan for item in parsed_dt]
        return "time", xs, "time"

    xs_num, ok = _to_float_list(column_values)
    if ok >= max(3, int(0.8 * len(column_values))):
        return "index", xs_num, "index"

    return "row", list(range(len(rows))), "row"


def _collect_numeric_columns(
    headers: List[str],
    rows: List[List[str]],
    skip_indices: Iterable[int],
) -> List[Tuple[int, str, List[float]]]:
    cols: List[Tuple[int, str, List[float]]] = []
    ncols = max(len(row) for row in rows) if rows else 0
    skips = set(skip_indices)

    for idx in range(ncols):
        if idx in skips:
            continue
        values = [row[idx] if len(row) > idx else "" for row in rows]
        floats, ok = _to_float_list(values)
        if ok >= max(2, int(0.6 * len(values))):
            name = headers[idx] if idx < len(headers) else f"col{idx}"
            cols.append((idx, name, floats))
    return cols


def _resolve_col_token(token: str, headers: List[str]) -> Optional[int]:
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)

    lowered = [header.lower() for header in headers]
    try:
        return lowered.index(token.lower())
    except ValueError:
        return None


def _resolve_output_path(csv_path: str, out_path: Optional[str]) -> str:
    base, _ = os.path.splitext(os.path.expanduser(csv_path))
    if out_path is None:
        return base + ".png"

    candidate = os.path.expanduser(out_path)
    if candidate.endswith(os.sep) or os.path.isdir(candidate):
        os.makedirs(candidate, exist_ok=True)
        return os.path.join(candidate, os.path.basename(base) + ".png")

    parent = os.path.dirname(candidate) or "."
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as exc:
            raise typer.BadParameter(f"Cannot create parent directory for out-path: {parent}: {exc}") from exc

    _root, ext = os.path.splitext(candidate)
    if ext.lower() != ".png":
        return candidate + ".png"
    return candidate


def _parse_xlim(xlim: Optional[str], total_points: int) -> Optional[Tuple[int, int]]:
    if xlim is None:
        return None

    raw = xlim.replace(" ", "")
    sep = "," if "," in raw else (":" if ":" in raw else None)
    if sep is None:
        raise typer.BadParameter("--xlim must be in form start,end (comma or colon)")

    parts = raw.split(sep)
    if len(parts) != 2:
        raise typer.BadParameter("--xlim expects exactly two numbers: start,end")

    try:
        start = int(parts[0]) if parts[0] != "" else 0
        end = int(parts[1]) if parts[1] != "" else total_points - 1
    except ValueError:
        raise typer.BadParameter("--xlim values must be integers")

    start = max(0, start)
    end = min(total_points - 1, end)
    if start > end:
        raise typer.BadParameter("Index trim invalid: start > end")
    return start, end


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
) -> None:
    """Plot CSV columns using pyqtgraph with subplots per data column."""
    data = _read_csv(csv_path, delimiter)
    ncols = max(len(row) for row in data.rows) if data.rows else 0

    if xcol is None:
        x_index = 0 if ncols > 0 else -1
    else:
        x_index = _resolve_col_token(xcol, data.headers)
        if x_index is None or x_index < 0 or x_index >= ncols:
            raise typer.BadParameter(f"--xcol '{xcol}' not found")

    x_kind, xs, _ = _column_as_x(data.rows, x_index) if x_index >= 0 else ("row", [], "row")
    total_original_points = len(xs)
    x_name = data.headers[x_index] if (0 <= x_index < len(data.headers)) else "row"

    if ycols:
        chosen_indices: List[int] = []
        for token in ycols.split(","):
            idx = _resolve_col_token(token, data.headers)
            if idx is None:
                warn(f"y column token '{token.strip()}' not found; skipping")
                continue
            if idx == x_index:
                warn(f"y column token '{token.strip()}' is xcol; skipping")
                continue
            if idx < 0 or idx >= ncols:
                warn(f"y column index {idx} out of range; skipping")
                continue
            if idx not in chosen_indices:
                chosen_indices.append(idx)

        ycols_list: List[Tuple[int, str, List[float]]] = []
        for idx in chosen_indices:
            values = [row[idx] if len(row) > idx else "" for row in data.rows]
            floats, ok = _to_float_list(values)
            name = data.headers[idx] if idx < len(data.headers) else f"col{idx}"
            if ok == 0:
                warn(f"column '{name}' has no numeric data; it will appear empty")
            ycols_list.append((idx, name, floats))
    else:
        ycols_list = _collect_numeric_columns(data.headers, data.rows, skip_indices=[x_index])

    if not ycols_list:
        raise typer.BadParameter("No Y columns to plot")

    selected_pairs = [(idx, name) for idx, name, _ in ycols_list]
    selected_display = [f"{name}[{idx}]" for idx, name in selected_pairs]
    all_candidate_indices = [i for i in range(len(data.headers)) if i != x_index]
    selected_indices_set = {idx for idx, _ in selected_pairs}
    unselected_pairs = [(i, data.headers[i]) for i in all_candidate_indices if i not in selected_indices_set]
    unselected_display = [f"{name}[{idx}]" for idx, name in unselected_pairs]

    print(f"[bold cyan]Loaded[/bold cyan] {len(data.rows)} rows, {len(data.headers)} columns from: {csv_path}")
    if 0 <= x_index < len(data.headers):
        print(f"Using x-axis: {x_kind} (column: {x_name}[{x_index}])")
    else:
        print(f"Using x-axis: {x_kind} (column: row index)")
    print(f"Y subplots: ({len(selected_pairs)} / {len(all_candidate_indices)})")
    print(f"Selected channels ({len(selected_display)}): " + (", ".join(selected_display) if selected_display else "(none)"))
    print(f"Unselected channels ({len(unselected_display)}): " + (", ".join(unselected_display) if unselected_display else "(none)"))

    trimmed_range = _parse_xlim(xlim, len(xs))
    if trimmed_range is not None:
        start, end = trimmed_range
        indices = range(start, end + 1)
        xs = [xs[i] for i in indices]
        ycols_list = [
            (idx, name, [arr[i] if i < len(arr) else math.nan for i in indices])
            for idx, name, arr in ycols_list
        ]

    if len(xs) == total_original_points:
        print(f"Data points (x): {len(xs)} (full dataset)")
    elif trimmed_range is not None:
        print(
            f"Data points (x): [{trimmed_range[0]},{trimmed_range[1]}] "
            f"({len(xs)} / {total_original_points} points selected)"
        )

    try:
        from PyQt6 import QtWidgets
        import pyqtgraph as pg
        from pyqtgraph import DateAxisItem
    except Exception as exc:
        raise typer.BadParameter(
            "PyQt6/pyqtgraph not available. Please install with: pip install PyQt6 pyqtgraph"
        ) from exc

    app_qt = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    pg.setConfigOption("background", (255, 255, 255))
    pg.setConfigOption("foreground", (0, 0, 0))

    win = pg.GraphicsLayoutWidget()
    win.setWindowTitle(title or f"CSV Plot - {os.path.basename(csv_path)}")
    win.resize(1000, 800)

    try:
        pal = win.palette()
        pal.setColor(pal.ColorRole.Window, QtWidgets.QColor(255, 255, 255))
        win.setPalette(pal)
    except Exception:
        pass

    try:
        win.ci.setSpacing(8, 3)
    except Exception:
        pass

    if not (weight > 0):
        raise typer.BadParameter("--weight must be > 0")

    pen_colors = [
        (33, 150, 243),
        (244, 67, 54),
        (76, 175, 80),
        (255, 152, 0),
        (156, 39, 176),
        (0, 121, 107),
    ]

    first_plot = None
    nplots = len(ycols_list)
    for i, (_idx, name, ys) in enumerate(ycols_list):
        is_last = i == nplots - 1
        axis_items = {"bottom": DateAxisItem(orientation="bottom")} if (x_kind == "time" and is_last) else None
        plot_item = win.addPlot(row=i, col=0, axisItems=axis_items)
        plot_item.setLabel("left", name)
        plot_item.showGrid(x=True, y=True, alpha=0.12)
        try:
            plot_item.getAxis("left").setPen("k")
            plot_item.getAxis("left").setTextPen("k")
            plot_item.getAxis("bottom").setPen("k")
            plot_item.getAxis("bottom").setTextPen("k")
        except Exception:
            pass

        if not is_last:
            try:
                plot_item.getAxis("bottom").setStyle(showValues=False)
                if hasattr(plot_item.getAxis("bottom"), "setHeight"):
                    plot_item.getAxis("bottom").setHeight(2)
            except Exception:
                pass
        elif x_kind == "time":
            plot_item.setLabel("bottom", "Time")
        elif x_kind == "index":
            plot_item.setLabel("bottom", x_name)
        else:
            plot_item.setLabel("bottom", "Index")

        if first_plot is not None:
            plot_item.setXLink(first_plot)

        color = pen_colors[i % len(pen_colors)]
        plot_item.plot(xs, ys, pen=pg.mkPen(color=color, width=weight))
        if first_plot is None:
            first_plot = plot_item

    app_qt.processEvents()

    if out_path is not None and not save:
        warn("--out-path provided without --save; enabling save mode.")
        save = True

    if save:
        win.show()
        app_qt.processEvents()
        resolved_out = _resolve_output_path(csv_path, out_path)
        try:
            from pyqtgraph.exporters import ImageExporter  # type: ignore
        except Exception as exc:
            raise typer.BadParameter(
                "Export requires pyqtgraph.exporters (ensure pyqtgraph is up to date)."
            ) from exc

        target_item = getattr(win, "ci", None) or first_plot
        exporter = ImageExporter(target_item)
        width_env = os.environ.get("WORD_EXPORT_WIDTH", "2400")
        per_plot_env = os.environ.get("WORD_EXPORT_PER_PLOT", "210")
        try:
            width_px = int(width_env)
            per_plot_height = int(per_plot_env)
        except ValueError:
            warn("WORD_EXPORT_WIDTH or WORD_EXPORT_PER_PLOT invalid; using defaults (2400, 210)")
            width_px = 2400
            per_plot_height = 210

        exporter.parameters()["width"] = width_px
        exporter.parameters()["height"] = max(600, per_plot_height * nplots)
        try:
            exporter.export(resolved_out)
            print(
                f"[green]Saved PNG[/green]: {resolved_out} "
                f"(width={width_px}px, plots={nplots}, line-width={weight})"
            )
        except Exception as exc:
            raise typer.BadParameter(f"Failed export: {exc}") from exc
        return

    try:
        from PyQt6 import QtCore, QtGui  # type: ignore

        esc_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Escape"), win)
        esc_shortcut.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        esc_shortcut.activated.connect(win.close)  # type: ignore[arg-type]
    except Exception:
        pass

    win.show()
    print("[dim]Press ESC to close the window.[/dim]")
    app_qt.exec()
