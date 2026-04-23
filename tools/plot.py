"""CSV plotting command implementation for `plot`."""

from __future__ import annotations

import csv
import json
import math
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

import click
import typer
from rich import print

try:
    from ._cli_common import new_typer_app
    from ._cli_output import warn
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_common import new_typer_app
    from tools._cli_output import warn

try:
    # Optional but highly recommended for robust datetime parsing.
    import dateutil.parser as dateparser  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    dateparser = None


app = new_typer_app(context_settings={"allow_interspersed_args": True})


_CONFIG_OPTION_KEYS = {
    "delimiter",
    "title",
    "scale",
    "export",
    "out_path",
    "xcol",
    "ycols",
    "xlim",
    "weight",
    "points_only",
}
_DEFAULT_PLOT_CONFIG_PATH = os.path.expanduser("~/.config/lf-toolbox/plot.defaults.json")


def _default_plot_config_payload() -> Dict[str, object]:
    return {
        "delimiter": None,
        "title": None,
        "scale": 1.0,
        "export": False,
        "out_path": None,
        "xcol": None,
        "ycols": None,
        "xlim": None,
        "weight": 1.0,
        "points_only": False,
    }


def _ensure_plot_config_file(path: str) -> bool:
    expanded_path = os.path.expanduser(path)
    if os.path.isfile(expanded_path):
        return False

    parent = os.path.dirname(expanded_path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise typer.BadParameter(f"Cannot create config directory '{parent}': {exc}") from exc

    try:
        with open(expanded_path, "w", encoding="utf-8") as handle:
            json.dump(_default_plot_config_payload(), handle, indent=2)
            handle.write("\n")
    except OSError as exc:
        raise typer.BadParameter(f"Cannot write config file '{expanded_path}': {exc}") from exc
    return True


def _open_path_for_edit(path: str) -> None:
    expanded_path = os.path.expanduser(path)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    try:
        if editor:
            editor_cmd = shlex.split(editor)
            if not editor_cmd:
                raise typer.BadParameter("EDITOR/VISUAL is set but empty")
            subprocess.Popen([*editor_cmd, expanded_path])
            return

        if sys.platform == "darwin":
            subprocess.Popen(["open", expanded_path])
            return
        if os.name == "nt":
            os.startfile(expanded_path)  # type: ignore[attr-defined]
            return

        subprocess.Popen(["xdg-open", expanded_path])
    except OSError as exc:
        raise typer.BadParameter(f"Cannot open config file '{expanded_path}': {exc}") from exc


def _normalize_plot_config_value(key: str, value: object) -> object:
    optional_str_keys = {"delimiter", "title", "out_path", "xlim"}
    if key in optional_str_keys:
        if value is None or isinstance(value, str):
            return value
        raise typer.BadParameter(f"Config key '{key}' must be a string or null")

    if key == "xcol":
        if isinstance(value, str):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        raise typer.BadParameter("Config key 'xcol' must be a string or integer")

    if key == "ycols":
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            tokens: List[str] = []
            for idx, token in enumerate(value):
                if isinstance(token, str):
                    normalized = token.strip()
                elif isinstance(token, int) and not isinstance(token, bool):
                    normalized = str(token)
                else:
                    raise typer.BadParameter(
                        f"Config key 'ycols' list item at index {idx} must be a string or integer"
                    )
                if normalized:
                    tokens.append(normalized)
            return ",".join(tokens)
        raise typer.BadParameter("Config key 'ycols' must be a string or list")

    if key in {"export", "points_only"}:
        if isinstance(value, bool):
            return value
        raise typer.BadParameter(f"Config key '{key}' must be a boolean")

    if key in {"scale", "weight"}:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise typer.BadParameter(f"Config key '{key}' must be a number")

    raise typer.BadParameter(f"Unsupported config key: {key}")


def _load_plot_config(path: str) -> Dict[str, object]:
    expanded_path = os.path.expanduser(path)
    if not os.path.isfile(expanded_path):
        raise typer.BadParameter(f"Config file not found: {expanded_path}")

    try:
        with open(expanded_path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON in config file '{expanded_path}': {exc.msg}") from exc
    except OSError as exc:
        raise typer.BadParameter(f"Cannot read config file '{expanded_path}': {exc}") from exc

    if not isinstance(loaded, dict):
        raise typer.BadParameter("Config file root must be a JSON object")

    unknown_keys = sorted(set(loaded.keys()) - _CONFIG_OPTION_KEYS)
    if unknown_keys:
        raise typer.BadParameter(f"Unsupported config keys: {', '.join(unknown_keys)}")

    normalized: Dict[str, object] = {}
    for key, value in loaded.items():
        normalized[key] = _normalize_plot_config_value(key, value)
    return normalized


def _merge_plot_options(
    cli_values: Dict[str, object],
    config_values: Dict[str, object],
    cli_explicit_keys: Iterable[str],
) -> Dict[str, object]:
    # Merge precedence is order-sensitive:
    # 1) start with Typer-resolved runtime values (defaults + CLI),
    # 2) apply config values only for options that were not explicitly passed,
    # 3) preserve explicit CLI entries so one-off overrides always win.
    merged = dict(cli_values)
    explicit = set(cli_explicit_keys)
    for key, value in config_values.items():
        if key in explicit:
            continue
        merged[key] = value
    return merged


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


def _parse_xlim(xlim: Optional[str]) -> Optional[Tuple[Optional[float], Optional[float]]]:
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
        start = float(parts[0]) if parts[0] != "" else None
        end = float(parts[1]) if parts[1] != "" else None
    except ValueError:
        raise typer.BadParameter("--xlim values must be numbers")

    if start is not None and not math.isfinite(start):
        raise typer.BadParameter("--xlim start must be finite")
    if end is not None and not math.isfinite(end):
        raise typer.BadParameter("--xlim end must be finite")
    if start is not None and end is not None and start > end:
        raise typer.BadParameter("X range invalid: start > end")
    return start, end


def _filter_scaled_xlim(
    xs: List[float],
    ycols_list: List[Tuple[int, str, List[float]]],
    xlim: Optional[Tuple[Optional[float], Optional[float]]],
) -> Tuple[
    List[float],
    List[Tuple[int, str, List[float]]],
    Optional[Tuple[float, float]],
    Optional[Tuple[float, float]],
]:
    if xlim is None:
        return xs, ycols_list, None, None

    # Clamp requested bounds to finite data extents first, then apply an inclusive
    # value-based filter on scaled x values. This keeps oversized requested ranges
    # usable while still failing clearly when no data point falls in the final range.
    finite_xs = [x for x in xs if math.isfinite(x)]
    if not finite_xs:
        raise typer.BadParameter("Cannot apply --xlim: x-axis has no finite values")

    data_min = min(finite_xs)
    data_max = max(finite_xs)
    req_start, req_end = xlim
    start = data_min if req_start is None else min(max(req_start, data_min), data_max)
    end = data_max if req_end is None else min(max(req_end, data_min), data_max)
    if start > end:
        raise typer.BadParameter("X trim invalid after clamping: start > end")

    selected_indices = [idx for idx, x in enumerate(xs) if math.isfinite(x) and start <= x <= end]
    if not selected_indices:
        raise typer.BadParameter(
            f"--xlim selected no data points after clamping (data range: [{data_min:.6g},{data_max:.6g}])"
        )

    filtered_xs = [xs[i] for i in selected_indices]
    filtered_ycols = [
        (idx, name, [arr[i] if i < len(arr) else math.nan for i in selected_indices])
        for idx, name, arr in ycols_list
    ]
    return filtered_xs, filtered_ycols, (start, end), (data_min, data_max)


def _nearest_finite_sample_index(xs: List[float], ys: List[float], target_x: float) -> Optional[int]:
    """Find nearest index by x where both x and y are finite."""
    nearest_idx: Optional[int] = None
    nearest_dist = math.inf
    for idx, x_val in enumerate(xs):
        if idx >= len(ys):
            continue
        y_val = ys[idx]
        if not (math.isfinite(x_val) and math.isfinite(y_val)):
            continue
        distance = abs(x_val - target_x)
        if distance < nearest_dist:
            nearest_dist = distance
            nearest_idx = idx
    return nearest_idx


def _format_x_value(x_value: float, x_kind: str) -> str:
    if not math.isfinite(x_value):
        return "nan"
    if x_kind == "time":
        try:
            return datetime.fromtimestamp(x_value).isoformat(sep=" ", timespec="seconds")
        except Exception:
            return f"{x_value:.6g}"
    return f"{x_value:.6g}"


@app.callback()
def plot(
    csv_path: Optional[str] = typer.Argument(None, help="Path to CSV file (optional for --config/--config-show)."),
    config: bool = typer.Option(
        False,
        "-c",
        "--config",
        help="Load defaults from ~/.config/lf-toolbox/plot.defaults.json. Explicit CLI flags override config values.",
    ),
    config_show: bool = typer.Option(
        False,
        "--config-show",
        help="Create/open ~/.config/lf-toolbox/plot.defaults.json in your editor and exit.",
    ),
    delimiter: Optional[str] = typer.Option(None, "-d", "--delimiter", help="CSV delimiter (auto if omitted)"),
    title: Optional[str] = typer.Option(None, "-t", "--title", help="Window title"),
    scale: float = typer.Option(
        1.0,
        "-s",
        "--scale",
        help="Multiply plotted X values by this factor (e.g. frame_id to seconds: 0.02 at 50 Hz).",
    ),
    export: bool = typer.Option(False, "-e", "--export", help="Export a high-resolution PNG (ImageExporter) and exit."),
    out_path: Optional[str] = typer.Option(
        None,
        "-o",
        "--out-path",
        help="Output PNG file path or directory when using --export. If a directory or ends with a path separator, the file name <csv_basename>.png is used. Extension .png will be appended if missing. Providing this flag implies --export if not explicitly set.",
    ),
    xcol: Optional[str] = typer.Option(None, "-x", "--xcol", help="Column (name or index) to use as X axis (time/index). Default: auto from first column"),
    ycols: Optional[str] = typer.Option(None, "-y", "--ycols", help="Comma-separated columns (names or indices) for Y subplots. Default: all numeric except xcol"),
    xlim: Optional[str] = typer.Option(
        None,
        "--xlim",
        help="Scaled x-value range start,end inclusive (e.g. 10.5,22.0). Accepts comma or colon.",
    ),
    weight: float = typer.Option(
        1.0,
        "-w",
        "--weight",
        help="Stroke width/point size in pixels for line and points-only modes (default: 1.0).",
    ),
    points_only: bool = typer.Option(False, "-p", "--points-only", help="Plot points only (no connecting line)."),
) -> None:
    """Plot CSV columns using pyqtgraph with subplots per data column."""
    option_names = [
        "delimiter",
        "title",
        "scale",
        "export",
        "out_path",
        "xcol",
        "ycols",
        "xlim",
        "weight",
        "points_only",
    ]
    cli_values: Dict[str, object] = {
        "delimiter": delimiter,
        "title": title,
        "scale": scale,
        "export": export,
        "out_path": out_path,
        "xcol": xcol,
        "ycols": ycols,
        "xlim": xlim,
        "weight": weight,
        "points_only": points_only,
    }

    # Config mode has two explicit states:
    # 1) --config-show always ensures + opens the default file, then exits.
    # 2) --config loads defaults from that file only when plotting is requested.
    # This ordering keeps edit-first workflow predictable and avoids accidental
    # plotting when the user's intent is to modify presets.
    if config_show:
        created = _ensure_plot_config_file(_DEFAULT_PLOT_CONFIG_PATH)
        if created:
            print(f"[green]Created default plot config[/green]: {_DEFAULT_PLOT_CONFIG_PATH}")
        _open_path_for_edit(_DEFAULT_PLOT_CONFIG_PATH)
        print(f"Opened plot config: {_DEFAULT_PLOT_CONFIG_PATH}")
        raise typer.Exit()

    if config and csv_path is None:
        created = _ensure_plot_config_file(_DEFAULT_PLOT_CONFIG_PATH)
        if created:
            print(f"[green]Created default plot config[/green]: {_DEFAULT_PLOT_CONFIG_PATH}")
        _open_path_for_edit(_DEFAULT_PLOT_CONFIG_PATH)
        print(f"Opened plot config: {_DEFAULT_PLOT_CONFIG_PATH}")
        raise typer.Exit()

    if config:
        _ensure_plot_config_file(_DEFAULT_PLOT_CONFIG_PATH)
        click_ctx = click.get_current_context(silent=True)
        explicit_keys: set[str] = set()
        if click_ctx is not None:
            for name in option_names:
                source = click_ctx.get_parameter_source(name)
                if source == click.core.ParameterSource.COMMANDLINE:
                    explicit_keys.add(name)

        config_values = _load_plot_config(_DEFAULT_PLOT_CONFIG_PATH)
        merged = _merge_plot_options(cli_values, config_values, explicit_keys)
        delimiter = merged["delimiter"]  # type: ignore[assignment]
        title = merged["title"]  # type: ignore[assignment]
        scale = merged["scale"]  # type: ignore[assignment]
        export = merged["export"]  # type: ignore[assignment]
        out_path = merged["out_path"]  # type: ignore[assignment]
        xcol = merged["xcol"]  # type: ignore[assignment]
        ycols = merged["ycols"]  # type: ignore[assignment]
        xlim = merged["xlim"]  # type: ignore[assignment]
        weight = merged["weight"]  # type: ignore[assignment]
        points_only = merged["points_only"]  # type: ignore[assignment]
        print(f"Loaded plot config: {_DEFAULT_PLOT_CONFIG_PATH}")

    if csv_path is None:
        raise typer.BadParameter("FILE is required unless --config or --config-show is used.")

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
    if not math.isfinite(scale) or math.isclose(scale, 0.0, abs_tol=1e-12):
        raise typer.BadParameter("--scale must be a finite non-zero number")
    scale_enabled = not math.isclose(scale, 1.0, rel_tol=0.0, abs_tol=1e-12)
    if x_kind == "time" and scale_enabled:
        raise typer.BadParameter("--scale is not supported with datetime-like x-axis values")

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
    if scale_enabled:
        print(f"Applying x-axis scale: x_plot = x_raw * {scale:.6g}")
    print(f"Y subplots: ({len(selected_pairs)} / {len(all_candidate_indices)})")
    print(f"Selected channels ({len(selected_display)}): " + (", ".join(selected_display) if selected_display else "(none)"))
    print(f"Unselected channels ({len(unselected_display)}): " + (", ".join(unselected_display) if unselected_display else "(none)"))

    requested_xlim = _parse_xlim(xlim)
    if scale_enabled:
        xs = [x * scale if math.isfinite(x) else x for x in xs]
    xs, ycols_list, applied_xlim, data_xlim = _filter_scaled_xlim(xs, ycols_list, requested_xlim)

    if requested_xlim is None and len(xs) == total_original_points:
        print(f"Data points (x): {len(xs)} (full dataset)")
    elif applied_xlim is not None:
        req_start, req_end = requested_xlim
        req_start_txt = "" if req_start is None else f"{req_start:.6g}"
        req_end_txt = "" if req_end is None else f"{req_end:.6g}"
        print(
            f"Data points (x): [{applied_xlim[0]:.6g},{applied_xlim[1]:.6g}] "
            f"({len(xs)} / {total_original_points} points selected)"
        )
        if data_xlim is not None and (
            (req_start is not None and not math.isclose(req_start, applied_xlim[0], rel_tol=0.0, abs_tol=1e-12))
            or (req_end is not None and not math.isclose(req_end, applied_xlim[1], rel_tol=0.0, abs_tol=1e-12))
        ):
            print(
                f"--xlim [{req_start_txt},{req_end_txt}] clamped to data range "
                f"[{data_xlim[0]:.6g},{data_xlim[1]:.6g}]"
            )

    try:
        from PyQt6 import QtCore, QtWidgets
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
    plot_items: List[object] = []
    subplot_red_labels: List[object] = []
    subplot_blue_labels: List[object] = []
    nplots = len(ycols_list)
    render_mode = "points-only" if points_only else "line"

    # Keep subplot labels pinned to stable corners across zoom/pan.
    # Flow:
    # 1) place labels with small relative padding from current view bounds,
    # 2) skip invalid ranges (startup/empty ranges can be non-finite),
    # 3) re-run placement on every range change to preserve corner anchoring.
    def _position_subplot_labels(
        plot_item: object,
        name_label_item: object,
        red_label_item: object,
        blue_label_item: object,
    ) -> None:
        try:
            (x_min, x_max), (y_min, y_max) = plot_item.viewRange()
        except Exception:
            return

        if not all(math.isfinite(value) for value in (x_min, x_max, y_min, y_max)):
            return

        x_span = x_max - x_min
        y_span = y_max - y_min
        x_pad = (x_span * 0.02) if x_span > 0 else 1.0
        y_pad = (y_span * 0.05) if y_span > 0 else 1.0
        name_label_item.setPos(x_min + x_pad, y_max - y_pad)
        red_label_item.setPos(x_max - x_pad, y_max - y_pad)
        blue_label_item.setPos(x_max - x_pad, y_min + y_pad)

    for i, (_idx, name, ys) in enumerate(ycols_list):
        is_last = i == nplots - 1
        axis_items = {"bottom": DateAxisItem(orientation="bottom")} if (x_kind == "time" and is_last) else None
        plot_item = win.addPlot(row=i, col=0, axisItems=axis_items)
        plot_item.setLabel("left", "")
        plot_item.showGrid(x=True, y=True, alpha=0.12)
        try:
            plot_item.getAxis("left").setPen("k")
            plot_item.getAxis("left").setTextPen("k")
            plot_item.getAxis("bottom").setPen("k")
            plot_item.getAxis("bottom").setTextPen("k")
        except Exception:
            pass

        series_label = pg.TextItem(text=name, anchor=(0, 0), color=(0, 0, 0))
        series_label.setZValue(1100)
        red_marker_label = pg.TextItem(text="", anchor=(1, 0), color=(220, 20, 60))
        red_marker_label.setZValue(1101)
        blue_marker_label = pg.TextItem(text="", anchor=(1, 1), color=(25, 118, 210))
        blue_marker_label.setZValue(1101)
        # Keep corner labels out of auto-range bounds; otherwise range updates
        # can recursively expand limits while labels are re-anchored each frame.
        plot_item.addItem(series_label, ignoreBounds=True)
        plot_item.addItem(red_marker_label, ignoreBounds=True)
        plot_item.addItem(blue_marker_label, ignoreBounds=True)
        plot_item.vb.sigRangeChanged.connect(
            lambda *_args, item=plot_item, name_item=series_label, red_item=red_marker_label, blue_item=blue_marker_label: _position_subplot_labels(
                item,
                name_item,
                red_item,
                blue_item,
            )
        )

        if not is_last:
            try:
                plot_item.getAxis("bottom").setStyle(showValues=False)
                if hasattr(plot_item.getAxis("bottom"), "setHeight"):
                    plot_item.getAxis("bottom").setHeight(2)
            except Exception:
                pass
        elif x_kind == "time":
            plot_item.setLabel("bottom", "Time")
        else:
            base_x_label = x_name if x_kind == "index" else "Index"
            x_label = f"{base_x_label} * {scale:.6g}" if scale_enabled else base_x_label
            plot_item.setLabel("bottom", x_label)

        if first_plot is not None:
            plot_item.setXLink(first_plot)

        color = pen_colors[i % len(pen_colors)]
        if points_only:
            plot_item.plot(
                xs,
                ys,
                pen=None,
                symbol="o",
                symbolSize=weight,
                symbolBrush=color,
                symbolPen=pg.mkPen(color=color, width=weight),
            )
        else:
            plot_item.plot(xs, ys, pen=pg.mkPen(color=color, width=weight))
        _position_subplot_labels(plot_item, series_label, red_marker_label, blue_marker_label)
        if first_plot is None:
            first_plot = plot_item
        plot_items.append(plot_item)
        subplot_red_labels.append(red_marker_label)
        subplot_blue_labels.append(blue_marker_label)

    app_qt.processEvents()

    if out_path is not None and not export:
        warn("--out-path provided without --export; enabling export mode.")
        export = True

    if export:
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
        width_env = os.environ.get("PLOT_EXPORT_WIDTH", "2400")
        per_plot_env = os.environ.get("PLOT_EXPORT_PER_PLOT", "210")
        try:
            width_px = int(width_env)
            per_plot_height = int(per_plot_env)
        except ValueError:
            warn("PLOT_EXPORT_WIDTH or PLOT_EXPORT_PER_PLOT invalid; using defaults (2400, 210)")
            width_px = 2400
            per_plot_height = 210

        exporter.parameters()["width"] = width_px
        exporter.parameters()["height"] = max(600, per_plot_height * nplots)
        try:
            exporter.export(resolved_out)
            save_details = (
                f"(width={width_px}px, plots={nplots}, mode={render_mode}, line-width={weight})"
                if not points_only
                else f"(width={width_px}px, plots={nplots}, mode={render_mode}, point-size={weight})"
            )
            print(
                f"[green]Saved PNG[/green]: {resolved_out} "
                f"{save_details}"
            )
        except Exception as exc:
            raise typer.BadParameter(f"Failed export: {exc}") from exc
        return

    if first_plot is not None and plot_items:
        marker_state: Dict[str, Dict[str, object]] = {
            "left": {
                "color": (220, 20, 60),
                "lines": [],
            },
            "right": {
                "color": (25, 118, 210),
                "lines": [],
            },
        }

        # Click handling is order-sensitive:
        # 1) detect which subplot received the scene click,
        # 2) map that scene point to data-space X on that subplot,
        # 3) update one marker channel (left/red or right/blue),
        # 4) mirror line+value annotations onto every subplot at the same X.
        def _resolve_clicked_x(scene_pos: object) -> Optional[float]:
            for plot_item in plot_items:
                if plot_item.sceneBoundingRect().contains(scene_pos):
                    return float(plot_item.vb.mapSceneToView(scene_pos).x())
            return None

        def _ensure_marker_items(channel: str) -> None:
            state = marker_state[channel]
            lines = state["lines"]
            if lines:
                return

            color = state["color"]
            for plot_item in plot_items:
                line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(color=color, width=1.5))
                line.setZValue(1000)
                plot_item.addItem(line)
                lines.append(line)

        def _update_markers(channel: str, clicked_x: float) -> None:
            _ensure_marker_items(channel)
            state = marker_state[channel]
            lines = state["lines"]
            x_display = _format_x_value(clicked_x, x_kind)
            channel_labels = subplot_red_labels if channel == "left" else subplot_blue_labels
            channel_name = "red line" if channel == "left" else "blue line"

            # Marker updates are channel-specific:
            # - move the selected channel's vertical lines on all subplots,
            # - refresh only that corner label (top-right red / bottom-right blue),
            # - preserve the opposite channel label text until that channel updates.
            for idx, plot_item in enumerate(plot_items):
                lines[idx].setPos(clicked_x)

                _series_idx, _series_name, ys = ycols_list[idx]
                nearest_idx = _nearest_finite_sample_index(xs, ys, clicked_x)
                if nearest_idx is None:
                    y_value = math.nan
                else:
                    y_value = ys[nearest_idx]

                y_display = "nan" if not math.isfinite(y_value) else f"{y_value:.6g}"
                channel_labels[idx].setText(f"{channel_name}: {y_display} @ x={x_display}")

        def _on_scene_click(event: object) -> None:
            try:
                button = event.button()
            except Exception:
                return

            if button == QtCore.Qt.MouseButton.LeftButton:
                channel = "left"
            elif button == QtCore.Qt.MouseButton.RightButton:
                channel = "right"
            else:
                return

            clicked_x = _resolve_clicked_x(event.scenePos())
            if clicked_x is None or not math.isfinite(clicked_x):
                return

            _update_markers(channel, clicked_x)
            try:
                event.accept()
            except Exception:
                pass

        first_plot.scene().sigMouseClicked.connect(_on_scene_click)

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


if __name__ == "__main__":
    app()
