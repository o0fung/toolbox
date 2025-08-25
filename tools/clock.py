"""CLI: Full-screen seven-segment digital clock rendered with Rich.

- Uses Rich Live to continuously update the screen once per second.
- Renders digits using a simple seven-segment layout built from block characters.
"""

import typer
import time
import math
import re

from datetime import datetime

from rich.live import Live
from rich.text import Text
from rich.align import Align
from rich.console import Console


# User can access help message with shortcut -h
app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})
console = Console()     # For getting console size


# Callback that renders the live clock only when no subcommand is provided
@app.callback(invoke_without_command=True)
def clock(
    ctx: typer.Context,
    color: str = typer.Option("cyan", "-c", "--color", help="Digit color (Rich style, e.g., cyan, magenta, #00ffcc)"),
    size: str = typer.Option("medium", "-s", "--size", help="Digit size preset: small|medium|large|xlarge|xxlarge|xxxlarge"),
):
    """Run a full-screen digital clock UI until interrupted (Ctrl+C).

    Uses Rich Live to repaint the screen at ~1 FPS while aligning the rendered
    text in the center of the terminal window.
    """
    # If a subcommand (timer, countdown) is invoked, do nothing here.
    if ctx.invoked_subcommand:
        return

    # Live update of the time text message
    with Live(screen=True, refresh_per_second=10) as live:
        try:
            while True:
                # Get current timestamp
                now = datetime.now()

                # Update the time clock display
                renderable = _render_centered_text(now.strftime("%H:%M:%S"), color=color, size=size)
                live.update(renderable)
                
                # Tick on second boundaries (smoothly aligns updates to :00 milliseconds)
                time.sleep(1 - (time.time() % 1))

        except KeyboardInterrupt:
            pass


@app.command("countdown")
def countdown(
    values: list[str] = typer.Argument(..., help="Time as S | H M | H M S. Separators like space, ':', ',', '/' are accepted."),
    color: str = typer.Option("cyan", "-c", "--color", help="Digit color (Rich style, e.g., cyan, magenta, #00ffcc)"),
    size: str = typer.Option("medium", "-s", "--size", help="Digit size preset: small|medium|large|xlarge|xxlarge|xxxlarge"),
):
    """Run a countdown for the specified time, then hold at 00:00:00 until Ctrl+C.

    Parsing rules:
    - One integer: seconds
    - Two integers: hours minutes
    - Three integers: hours minutes seconds
    - Any non-digit separators (including spaces, ":", ",", "/") are accepted.
    """
    # Get countdown start time configuration
    # Join tokens to support both multi-arg (e.g., 1 10) and single-arg (e.g., 1:10)
    total = _parse_countdown_spec(" ".join(values).strip())

    start = time.monotonic()    # Baseline time origin, i.e., the current timestamp
    end = start + total         # End point of the time, i.e., the target timestamp

    # Live update of the time text message
    with Live(screen=True, refresh_per_second=10) as live:
        try:
            # Initiate remaining countdown time duration
            last_remaining: int | None = None

            while True:
                # Get current timestamp
                now = time.monotonic()

                # Calculate remaining seconds, clamp to zero
                remaining = max(0, int(math.ceil(end - now)))

                # Only redraw the display if the remaining value changed
                if remaining != last_remaining:
                    live.update(_render_centered_text(_format_hms(remaining), color=color, size=size))
                    last_remaining = remaining

                if remaining == 0:
                    # Hold at 00:00:00 until interrupted
                    time.sleep(0.1)
                    continue

                # Sleep until the next decrement boundary for smooth updates
                dt = end - (remaining - 1) - time.monotonic()
                if dt > 0:
                    time.sleep(dt)

        except KeyboardInterrupt:
            pass


@app.command("timer")
def timer(
    color: str = typer.Option("cyan", "-c", "--color", help="Digit color (Rich style, e.g., cyan, magenta, #00ffcc)"),
    size: str = typer.Option("medium", "-s", "--size", help="Digit size preset: small|medium|large|xlarge|xxlarge|xxxlarge"),
):
    """Run a stopwatch that counts up from 00:00:00 until interrupted (Ctrl+C)."""

    start = time.monotonic()        # Baseline time origin, i.e., the current timestamp
    
    # Live update of the time text message
    with Live(screen=True, refresh_per_second=10) as live:
        try:
            while True:
                # Get Elaspse time and update display
                elapsed = int(time.monotonic() - start)
                renderable = _render_centered_text(_format_hms(elapsed), color=color, size=size)
                live.update(renderable)

                # Align updates to second boundaries relative to start time
                time.sleep(max(0.0, 1 - ((time.monotonic() - start) % 1)))

        except KeyboardInterrupt:
            pass


def _format_hms(total_seconds: int) -> str:
    """Return time string HH:MM:SS for a non-negative total seconds value.

    Hours are zero-padded to at least 2 digits but grow as needed (e.g., 100:00:00).
    Negative inputs are clamped to 0.
    """
    if total_seconds < 0:
        total_seconds = 0
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _render_centered_text(text: str, color: str, size: str) -> Align:
    # Get user specific size preset configuration
    key = str(size).lower().strip()
    if key not in SIZE_PRESETS:
        raise typer.BadParameter(
            "Invalid size. Choose from: " + ", ".join(SIZE_PRESETS.keys())
        )
    preset = SIZE_PRESETS[key]
    
    # Display target timestamp
    big = _render_big_time(
        text,
    inner=preset["inner"],
    vthick=preset["vthick"],
        gap=preset["gap"],
    )

    # Center horizontally and vertically to fill the screen
    renderable = Align(
        Text(big, style=f"bold {color}"),
        align="center",
        vertical="middle",
        height=console.size.height,
        width=console.size.width,
    )
    return renderable


def _parse_countdown_spec(spec: str) -> int:
    """Parse countdown string into total seconds with flexible separators.

    Rules:
    - One integer: seconds
    - Two integers: hours minutes
    - Three integers: hours minutes seconds
    - Accepts any non-digit separators (spaces, ':', ',', '/')
    """
    # Ensure user provided the time congfiguration
    if not spec:
        raise typer.BadParameter("Countdown requires 1 to 3 integers: S | H M | H M S")

    # Use regular expression to find user specified timer setting
    parts = re.findall(r"\d+", spec)
    if not parts:
        raise typer.BadParameter("Provide 1-3 integers: S | H M | H M S")

    # Get time setting in integers
    nums = list(map(int, parts))

    if len(nums) == 1:
        # Given seconds only
        s = nums[0]
        return s
    
    elif len(nums) == 2:
        # Given hours and minutes
        h, m = nums
        return h * 3600 + m * 60
    
    elif len(nums) == 3:
        # Given hours, minutes and seconds
        h, m, s = nums
        return h * 3600 + m * 60 + s
    
    else:
        # Wrong time configuration
        raise typer.BadParameter("Too many values. Use S | H M | H M S (e.g., 45 | 1 30 | 2 15 00)")


# Presets controlling both horizontal and vertical segment thickness
SIZE_PRESETS = {
    "small": {"inner": 4, "vthick": 1, "gap": 1},
    "medium": {"inner": 6, "vthick": 2, "gap": 1},
    "large": {"inner": 8, "vthick": 3, "gap": 1},
    "xlarge": {"inner": 10, "vthick": 4, "gap": 1},
    "xxlarge": {"inner": 12, "vthick": 5, "gap": 1},
    "xxxlarge": {"inner": 14, "vthick": 6, "gap": 1},
}

# Seven-segment layout for digits 0-9.
# Segment keys:
#   a: top horizontal
#   b: upper-right vertical
#   c: lower-right vertical
#   d: bottom horizontal
#   e: lower-left vertical
#   f: upper-left vertical
#   g: middle horizontal
SEGMENTS = {
    "0": {"a", "b", "c", "d", "e", "f"},
    "1": {"b", "c"},
    "2": {"a", "b", "g", "e", "d"},
    "3": {"a", "b", "g", "c", "d"},
    "4": {"f", "g", "b", "c"},
    "5": {"a", "f", "g", "c", "d"},
    "6": {"a", "f", "g", "e", "c", "d"},
    "7": {"a", "b", "c"},
    "8": {"a", "b", "c", "d", "e", "f", "g"},
    "9": {"a", "b", "c", "d", "f", "g"},
}


def _render_digit(
    segments: set[str], inner: int = 6, vthick: int = 2, vchar: str = "█", hchar: str = "█"
) -> list[str]:
    """Render a single seven-segment digit as a list of 7 text rows.

    Args:
        segments: Which segment labels (a-g) should be lit for the digit.
        inner: Horizontal thickness/width of the horizontal bars and the gap between vertical bars.
        vchar: Character to draw vertical bars.
        hchar: Character to draw horizontal bars.
    """
    # Rows layout: top, vthick x upper verts, middle, vthick x lower verts, bottom
    space_inner = " " * inner
    hbar = hchar * inner
    left = lambda on: vchar if on else " "
    right = lambda on: vchar if on else " "
    rows = []
    # a
    rows.append(" " + (hbar if "a" in segments else space_inner) + " ")
    # f, b (repeat for vertical thickness)
    for _ in range(vthick):
        rows.append(f"{left('f' in segments)}{space_inner}{right('b' in segments)}")
    # g
    rows.append(" " + (hbar if "g" in segments else space_inner) + " ")
    # e, c (repeat for vertical thickness)
    for _ in range(vthick):
        rows.append(f"{left('e' in segments)}{space_inner}{right('c' in segments)}")
    # d
    rows.append(" " + (hbar if "d" in segments else space_inner) + " ")
    return rows


def _render_colon(width: int, vthick: int, inner: int) -> list[str]:
    """Render a colon glyph occupying the same height as digits.

    Places two horizontal dots centered in width, aligned roughly with the
    upper and lower vertical segment blocks. Dot width scales mildly with size.
    """
    rows_count = 2 * vthick + 3
    rows = [" " * width for _ in range(rows_count)]
    mid = width // 2

    # Choose dot width based on horizontal thickness
    dot_w = 1 if inner <= 3 else (2 if inner <= 7 else 3)

    def put_dot(r: int, s: str) -> str:
        line = list(s)
        start = max(0, mid - dot_w // 2)
        end = min(len(line), start + dot_w)
        for c in range(start, end):
            line[c] = "█"
        return "".join(line)

    upper_row = 1 + (vthick // 2)
    lower_row = 1 + vthick + 1 + (vthick // 2)
    if 0 <= upper_row < rows_count:
        rows[upper_row] = put_dot(upper_row, rows[upper_row])
    if 0 <= lower_row < rows_count:
        rows[lower_row] = put_dot(lower_row, rows[lower_row])
    return rows


def _render_big_time(timestr: str, inner: int = 6, vthick: int = 2, gap: int = 1) -> str:
    """Render an entire time string (e.g., "12:34:56") as a multi-line banner.

    Args:
        timestr: String containing digits and ':' characters.
        inner: Thickness/spacing parameter passed to each digit.
        gap: Spaces between rendered glyphs.
    Returns:
        The multi-line string representing the large time.
    """
    glyphs: list[list[str]] = []
    digit_width = inner + 2
    rows_count = 2 * vthick + 3
    
    # Have input Glyph
    for ch in timestr:
        # Render digits
        if ch.isdigit():
            glyphs.append(_render_digit(SEGMENTS[ch], inner=inner, vthick=vthick))

        # Render colon symbol
        elif ch == ":":
            glyphs.append(_render_colon(digit_width, vthick=vthick, inner=inner))

    if not glyphs:
        return ""       # No input glyph, return nothing
    
    # Add gaps between glyphs
    lines = []
    spacer = " " * gap
    for r in range(rows_count):
        lines.append(spacer.join(g[r] for g in glyphs))

    return "\n".join(lines)


# Entry point for manual execution: `python tools/clock.py`
if __name__ == '__main__':
    app()
