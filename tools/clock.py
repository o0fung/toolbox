"""CLI: Full-screen seven-segment digital clock rendered with Rich.

- Uses Rich Live to continuously update the screen once per second.
- Renders digits using a simple seven-segment layout built from block characters.
"""

import typer
import time

from datetime import datetime

from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.text import Text


app = typer.Typer()
console = Console()


# Main CLI entry point, allows invocation without subcommand (entry from go.py)
@app.callback()

def clock():
    """Run a full-screen digital clock UI until interrupted (Ctrl+C).

    Uses Rich Live to repaint the screen at ~1 FPS while aligning the rendered
    text in the center of the terminal window.
    """
    with Live(screen=True, refresh_per_second=10) as live:
        try:
            while True:
                now = datetime.now()
                big = render_big_time(now.strftime("%H:%M:%S"), inner=6, gap=1)
                
                # Center horizontally and vertically to fill the screen
                renderable = Align(
                    Text(big, style="bold cyan"),
                    align="center",
                    vertical="middle",
                    height=console.size.height,
                    width=console.size.width,
                )
                live.update(renderable)
                
                # Tick on second boundaries (smoothly aligns updates to :00 milliseconds)
                time.sleep(1 - (time.time() % 1))

        except KeyboardInterrupt:
            pass


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


def render_digit(segments: set[str], inner: int = 6, vchar: str = "█", hchar: str = "█") -> list[str]:
    """Render a single seven-segment digit as a list of 7 text rows.

    Args:
        segments: Which segment labels (a-g) should be lit for the digit.
        inner: Horizontal thickness/width of the horizontal bars and the gap between vertical bars.
        vchar: Character to draw vertical bars.
        hchar: Character to draw horizontal bars.
    """
    # 7 rows: top, 2x upper verts, middle, 2x lower verts, bottom
    space_inner = " " * inner
    hbar = hchar * inner
    left = lambda on: vchar if on else " "
    right = lambda on: vchar if on else " "
    rows = []
    # a
    rows.append(" " + (hbar if "a" in segments else space_inner) + " ")
    # f, b (twice for thickness)
    for _ in range(2):
        rows.append(f"{left('f' in segments)}{space_inner}{right('b' in segments)}")
    # g
    rows.append(" " + (hbar if "g" in segments else space_inner) + " ")
    # e, c (twice)
    for _ in range(2):
        rows.append(f"{left('e' in segments)}{space_inner}{right('c' in segments)}")
    # d
    rows.append(" " + (hbar if "d" in segments else space_inner) + " ")
    return rows


def render_colon(width: int) -> list[str]:
    """Render a colon glyph occupying the same 7-row height as digits.

    The colon is two stacked squares placed around the middle rows.
    """
    # Same 7 rows; draw two dots
    rows = [" " * width for _ in range(7)]
    mid = width // 2

    def put_dot(r: int, s: str) -> str:
        line = list(s)
        for c in (mid - 1, mid):
            if 0 <= c < len(line):
                line[c] = "█"
                
        return "".join(line)
    
    rows[2] = put_dot(2, rows[2])
    rows[4] = put_dot(4, rows[4])
    return rows


def render_big_time(timestr: str, inner: int = 6, gap: int = 1) -> str:
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
    
    for ch in timestr:
        # Render digits
        if ch.isdigit():
            glyphs.append(render_digit(SEGMENTS[ch], inner=inner))
        # Render colon symbol
        elif ch == ":":
            glyphs.append(render_colon(digit_width))

    if not glyphs:
        return ""
    
    lines = []
    spacer = " " * gap
    for r in range(7):
        lines.append(spacer.join(g[r] for g in glyphs))

    return "\n".join(lines)


# Entry point for manual execution: `python tools/clock.py`
if __name__ == '__main__':
    typer.run(clock)
