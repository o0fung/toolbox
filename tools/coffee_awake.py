"""macOS sleep holds for ``lf coffee``.

Amphetamine is preferred only when it is installed, the app bundle can be
launched, and Session Defaults are already saved with closed-display sleep
off. A session is started with display sleep allowed so the panel can go
black. Amphetamine's AppleScript dictionary only accepts duration, interval,
and ``displaySleepAllowed`` on ``start new session``. The Session Defaults
checkbox "Allow system sleep when display is closed" is not a start-session
parameter. This module reads that preference and never writes it.

When Amphetamine is missing, not usable, or not configured for a closed lid,
callers print setup instructions and use ``sudo pmset -a disablesleep 1``.
``--pmset`` selects that path up front and does not require Amphetamine.
Plain ``caffeinate`` is intentionally not used: its assertions are dropped
when the lid closes.

``disablesleep`` is restored on normal release, and if this process dies, by
a detached supervisor plus the lock file. The next ``lf coffee`` / ``status``
/ ``stop`` repairs a lock whose owner is already gone. A hard reboot that
kills both processes is repaired the same way the next time coffee runs.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator


AMPHETAMINE_SESSION_HOURS = 12
END_SESSION_SCRIPT = 'tell application "Amphetamine" to end session'
END_SESSION_URL = "amphetamine://end-session"
_CLAMSHELL_RE = re.compile(
    r'"AppleClamshellState"\s*=\s*(Yes|No|true|false|[01])\b',
    re.IGNORECASE,
)


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        rendered = " ".join(self.argv)
        detail = stderr.strip()
        message = f"{rendered} failed ({returncode})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


Run = Callable[..., CommandResult]


def default_run(
    argv: Sequence[str],
    *,
    interactive: bool = False,
    timeout: float = 30,
) -> CommandResult:
    """Run a command. Interactive mode inherits the terminal so sudo can prompt."""
    try:
        if interactive:
            completed = subprocess.run(list(argv), check=False, timeout=timeout)
            stdout, stderr = "", ""
        else:
            completed = subprocess.run(
                list(argv),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError(list(argv), 1, str(exc)) from exc
    if completed.returncode != 0:
        raise CommandError(list(argv), completed.returncode, stderr or stdout)
    return CommandResult(completed.returncode, stdout, stderr)


@dataclass(frozen=True)
class LidState:
    position: str
    detail: str


@dataclass(frozen=True)
class PowerEventState:
    """Last system sleep and wake macOS recorded.

    These describe the machine, not coffee's sleep hold. ``wake_at`` is epoch
    seconds from ``kern.waketime``. None means that read failed, not that a
    wake just happened.
    """

    sleep_reason: str | None
    wake_reason: str | None
    wake_type: str | None
    user_wake: bool | None
    sleep_wake_id: str | None
    wake_at: float | None = None


@dataclass(frozen=True)
class PlatformState:
    """Lid plus the last sleep/wake from one poll."""

    lid: LidState
    power: PowerEventState


# IOPMrootDomain has the clamshell and the sleep/wake keys together. The
# second command is only a fallback when that class is missing.
_IOREG_ARGV = (
    ["ioreg", "-c", "IOPMrootDomain", "-d", "2"],
    ["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
)
_WAKETIME_RE = re.compile(r"sec\s*=\s*(\d+)(?:\D+?usec\s*=\s*(\d+))?")


def parse_clamshell_state(text: str) -> tuple[str, str | None]:
    """Parse ``AppleClamshellState`` from ``ioreg`` output.

    On a MacBook, IORegistry reports Yes/1 when the lid is closed and No/0
    when it is open (``IOPMrootDomain``). Desktops and some VMs omit the key.
    This is best-effort: a closed lid on an external display still reads closed,
    and a missing key is reported as unknown rather than guessed.
    """
    match = _CLAMSHELL_RE.search(text or "")
    if not match:
        return "unknown", None
    token = match.group(1)
    lowered = token.lower()
    if lowered in {"yes", "true", "1"}:
        return "closed", token
    if lowered in {"no", "false", "0"}:
        return "open", token
    return "unknown", token


def _iter_ioreg_text(run: Run) -> Iterator[str]:
    for argv in _IOREG_ARGV:
        try:
            result = run(argv, interactive=False, timeout=3)
        except CommandError:
            continue
        if result.stdout:
            yield result.stdout


def _ioreg_value(text: str, key: str) -> str | None:
    """Return one IORegistry property.

    Quoted values keep their inner text (``"Clamshell Sleep"``). Bare values
    such as ``Yes`` are taken up to the next whitespace.
    """
    match = re.search(
        rf'"{re.escape(key)}"\s*=\s*(?:"([^"]*)"|([^\s]+))',
        text or "",
    )
    if not match:
        return None
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    cleaned = raw.strip()
    return cleaned or None


def _yes_no(token: str | None) -> bool | None:
    if token is None:
        return None
    lowered = token.strip().lower()
    if lowered in {"yes", "true", "1"}:
        return True
    if lowered in {"no", "false", "0"}:
        return False
    return None


def parse_power_events(text: str) -> PowerEventState:
    """Parse the last sleep and wake out of an IOPMrootDomain dump.

    ``IOPMUserTriggeredFullWake`` is Yes when the user woke the Mac (lid,
    keyboard, trackpad, or power button) and No for a maintenance wake that
    leaves the screen off. A missing key stays None.
    """
    return PowerEventState(
        sleep_reason=_ioreg_value(text, "Last Sleep Reason"),
        wake_reason=_ioreg_value(text, "Wake Reason"),
        wake_type=_ioreg_value(text, "Wake Type"),
        user_wake=_yes_no(_ioreg_value(text, "IOPMUserTriggeredFullWake")),
        sleep_wake_id=_ioreg_value(text, "SleepWakeUUID"),
    )


def parse_waketime(text: str) -> float | None:
    """Parse ``sysctl kern.waketime`` into epoch seconds.

    The line looks like ``{ sec = 1792885281, usec = 416992 } Sun Oct 25 ...``.
    """
    match = _WAKETIME_RE.search(text or "")
    if not match:
        return None
    seconds = int(match.group(1))
    usec = int(match.group(2) or "0")
    return seconds + usec / 1_000_000


def read_waketime(run: Run = default_run) -> float | None:
    try:
        result = run(["sysctl", "-n", "kern.waketime"], interactive=False, timeout=3)
    except CommandError:
        return None
    return parse_waketime(result.stdout)


def _power_identity(state: PowerEventState) -> bool:
    return bool(state.sleep_wake_id or state.sleep_reason or state.wake_reason)


def read_platform_state(run: Run = default_run) -> PlatformState:
    """Read the lid and the last sleep/wake.

    One IOPMrootDomain dump usually has both the clamshell and the sleep
    keys. Desktops omit the clamshell; the lid stays unknown and the sleep
    keys are still kept. Wake time is ``kern.waketime``, read once per call.
    """
    lid = LidState("unknown", "best-effort; AppleClamshellState not available")
    power = parse_power_events("")
    for text in _iter_ioreg_text(run):
        position, token = parse_clamshell_state(text)
        if lid.position == "unknown" and token is not None and position != "unknown":
            lid = LidState(position, f"best-effort; AppleClamshellState={token}")
        events = parse_power_events(text)
        if not _power_identity(power) and _power_identity(events):
            power = events
        # Stop once this dump answered both questions. A later dump is only
        # a fallback for machines that split the keys.
        if lid.position != "unknown" and power.sleep_wake_id:
            break
    return PlatformState(lid=lid, power=replace(power, wake_at=read_waketime(run)))


def read_lid_state(run: Run = default_run) -> LidState:
    """Read the lid via ioreg. Failures become ``unknown`` with a best-effort note."""
    for text in _iter_ioreg_text(run):
        position, token = parse_clamshell_state(text)
        if position != "unknown" and token is not None:
            return LidState(position, f"best-effort; AppleClamshellState={token}")
    return LidState("unknown", "best-effort; AppleClamshellState not available")


_CONNECTION_RE = re.compile(r"Connection Type:\s*(.+)", re.IGNORECASE)
_WIFI_PORT_RE = re.compile(
    r"Hardware Port:\s*(?:Wi-Fi|AirPort)\s*\nDevice:\s*(\S+)",
    re.IGNORECASE,
)
_AIRPORT_POWER_RE = re.compile(r":\s*(On|Off)\s*$", re.IGNORECASE | re.MULTILINE)
_DISPLAYS_ARGV = ["system_profiler", "SPDisplaysDataType"]
_WIFI_PORTS_ARGV = ["networksetup", "-listallhardwareports"]


@dataclass(frozen=True)
class ClosedLidNetwork:
    """Display and Wi-Fi facts for one closed-lid poll.

    ``external_display`` is None when the display list could not be read.
    True is Apple's clamshell setup (an external display is connected).
    ``wifi_note`` is the phrase for the status line. None means Wi-Fi was
    not read on this poll.
    """

    external_display: bool | None
    wifi_note: str | None


def parse_external_display(text: str) -> bool | None:
    """Return whether ``system_profiler`` shows a display that is not built in.

    ``Internal`` is the laptop panel. Any other connection type is an
    external display, which is what turns lid-close into clamshell mode.
    No connection lines means the read did not answer the question.
    """
    kinds = [match.group(1).strip().lower() for match in _CONNECTION_RE.finditer(text or "")]
    if not kinds:
        return None
    return any(kind not in {"internal", "built-in"} for kind in kinds)


def parse_wifi_device(text: str) -> str | None:
    match = _WIFI_PORT_RE.search(text or "")
    return match.group(1) if match else None


def parse_airport_power(text: str) -> str | None:
    """Return ``on`` or ``off`` from ``networksetup -getairportpower``."""
    match = _AIRPORT_POWER_RE.search(text or "")
    if not match:
        return None
    return match.group(1).lower()


def parse_has_default_route(text: str) -> bool:
    return "gateway:" in (text or "").lower()


def wifi_note(*, power: str | None, restored: bool, has_route: bool) -> str:
    """Phrase for the lid line.

    Restored means this poll turned the radio back on. A radio that is on
    with no default route, or a radio that stayed off, is still down.
    """
    if restored:
        return "Wi-Fi was off; turned it back on"
    if power == "on" and has_route:
        return "Wi-Fi on"
    if power == "on" or power == "off":
        return "Wi-Fi still down"
    return "Wi-Fi unreadable"


def _capture(run: Run, argv: Sequence[str], *, timeout: float = 20) -> CommandResult | None:
    try:
        return run(argv, interactive=False, timeout=timeout)
    except CommandError:
        return None


def _read_wifi_parts(run: Run) -> tuple[str | None, str | None, bool]:
    """Return ``(device, power, has_route)``. Missing pieces stay None/False."""
    ports = _capture(run, _WIFI_PORTS_ARGV, timeout=5)
    device = parse_wifi_device(ports.stdout) if ports is not None else None
    power: str | None = None
    if device:
        reading = _capture(run, ["networksetup", "-getairportpower", device], timeout=5)
        if reading is not None:
            power = parse_airport_power(reading.stdout)
    route = _capture(run, ["route", "-n", "get", "default"], timeout=5)
    has_route = route is not None and parse_has_default_route(route.stdout)
    return device, power, has_route


def _power_wifi_on(run: Run, device: str) -> bool:
    """Turn the Wi-Fi radio on. ``sudo -n`` is the fallback when the first try needs admin.

    Interactive sudo is not used. A password prompt would stall the poll.
    """
    if _capture(run, ["networksetup", "-setairportpower", device, "on"], timeout=10) is not None:
        return True
    return _capture(run, ["sudo", "-n", "networksetup", "-setairportpower", device, "on"], timeout=10) is not None


def assess_closed_lid_network(run: Run = default_run, *, restore_wifi: bool = False) -> ClosedLidNetwork:
    """Read clamshell-vs-closed-lid, and optionally turn Wi-Fi back on.

    An external display is real clamshell: macOS keeps the session awake and
    coffee does not power the radio. With no display (or an unreadable
    display list), and ``restore_wifi``, a radio macOS turned off is switched
    back on. The radio can be on and still have no route; the note says so
    and the caller must not treat the agent as finished.
    """
    displays = _capture(run, _DISPLAYS_ARGV, timeout=20)
    external = parse_external_display(displays.stdout) if displays is not None else None
    # Clamshell already leaves the radio under macOS. Only the no-display
    # path, and only while an agent is busy, tries to turn Wi-Fi back on.
    should_restore = restore_wifi and external is not True
    device, power, has_route = _read_wifi_parts(run)
    restored = False
    if should_restore and power == "off" and device and _power_wifi_on(run, device):
        device, power, has_route = _read_wifi_parts(run)
        restored = power == "on"
    if external is True or should_restore:
        return ClosedLidNetwork(external, wifi_note(power=power, restored=restored, has_route=has_route))
    return ClosedLidNetwork(external, None)


def parse_power_source(text: str) -> str:
    """Return ``ac``, ``battery``, or ``unknown`` from ``pmset -g batt``.

    The draw source is the ``Now drawing from '...'`` line. The internal
    battery can be mentioned while the Mac is on AC, so the battery name
    alone is not enough.
    """
    lowered = (text or "").lower()
    if "now drawing from 'battery power'" in lowered or "now drawing from \"battery power\"" in lowered:
        return "battery"
    for line in lowered.splitlines():
        stripped = line.strip()
        if stripped == "battery power":
            return "battery"
    if "ac power" in lowered or "power adapter" in lowered:
        return "ac"
    return "unknown"


def parse_disablesleep(text: str) -> int | None:
    """Return 0/1 when ``pmset`` prints disablesleep, else None if the key is absent."""
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() in {"disablesleep", "sleepdisabled"}:
            try:
                return 0 if int(parts[-1]) == 0 else 1
            except ValueError:
                continue
    return None


def read_power_source(run: Run = default_run) -> str:
    try:
        result = run(["pmset", "-g", "batt"], interactive=False, timeout=5)
    except CommandError:
        return "unknown"
    return parse_power_source(result.stdout)


def read_disablesleep(run: Run = default_run) -> int | None:
    """Return the current disablesleep value.

    ``pmset`` omits the key when it is still at the default of 0, so a
    successful report without the key is 0. A failed ``pmset`` is None.
    """
    try:
        result = run(["pmset", "-g"], interactive=False, timeout=5)
    except CommandError:
        return None
    parsed = parse_disablesleep(result.stdout)
    if parsed is None:
        return 0 if result.stdout.strip() else None
    return parsed


def amphetamine_app_paths() -> tuple[Path, ...]:
    return (
        Path("/Applications/Amphetamine.app"),
        Path.home() / "Applications" / "Amphetamine.app",
    )


def amphetamine_installed(app_paths: Sequence[Path] | None = None) -> bool:
    paths = tuple(app_paths) if app_paths is not None else amphetamine_app_paths()
    return any(path.is_dir() for path in paths)


def amphetamine_bundle_usable(app_paths: Sequence[Path] | None = None) -> bool:
    """True when an Amphetamine.app bundle contains a launchable executable.

    A directory named ``Amphetamine.app`` with no ``Contents/MacOS`` binary is
    installed but not usable. Callers fall back to pmset instead of scripting it.
    """
    paths = tuple(app_paths) if app_paths is not None else amphetamine_app_paths()
    for path in paths:
        macos_dir = path / "Contents" / "MacOS"
        if not macos_dir.is_dir():
            continue
        if any(child.is_file() for child in macos_dir.iterdir()):
            return True
    return False


# App Store id for Amphetamine (William Gustafson). ``mas open`` and the public
# URL both land on this listing. coffee never installs or purchases the app.
AMPHETAMINE_APP_STORE_ID = "937984704"
AMPHETAMINE_APP_STORE_URL = "https://apps.apple.com/app/amphetamine/id937984704"
# Top-level preference written by Amphetamine when Session Defaults are saved.
# False means the checkbox "Allow system sleep when display is closed" is off,
# which is the closed-lid setup. True (or a missing key) is not that setup.
AMPHETAMINE_CLOSED_DISPLAY_SLEEP_KEY = "Allow Closed-Display Sleep"
AMPHETAMINE_BUNDLE_ID = "com.if.Amphetamine"
# One line. Printed when we open Amphetamine. The checkbox stays manual.
AMPHETAMINE_SESSION_DEFAULTS_NOTE = (
    'Uncheck Session Defaults → "Allow system sleep when display is closed" once. '
    "Amphetamine's scripting API cannot change that checkbox."
)


def amphetamine_preference_paths() -> tuple[Path, ...]:
    """Plists Amphetamine may have saved, container first.

    The App Store build is sandboxed, so the container file is the one the app
    reads. The user-domain plist is only consulted when that container file is
    absent. coffee never writes either file.
    """
    home = Path.home()
    container = (
        home
        / "Library"
        / "Containers"
        / AMPHETAMINE_BUNDLE_ID
        / "Data"
        / "Library"
        / "Preferences"
        / f"{AMPHETAMINE_BUNDLE_ID}.plist"
    )
    user_domain = home / "Library" / "Preferences" / f"{AMPHETAMINE_BUNDLE_ID}.plist"
    return (container, user_domain)


def read_amphetamine_preferences(paths: Sequence[Path] | None = None) -> dict[str, Any] | None:
    """Return the first readable Amphetamine plist, or None.

    The first path that exists and parses wins, so a sandboxed container file
    is not overridden by a stale user-domain defaults file. Unreadable files
    are skipped. This function does not create or modify plists.
    """
    candidates = tuple(paths) if paths is not None else amphetamine_preference_paths()
    for path in candidates:
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def parse_closed_display_sleep_allowed(preferences: dict[str, Any] | None) -> bool | None:
    """Return whether Amphetamine allows sleep when the display is closed.

    True means the Session Defaults checkbox is on (a closed lid may sleep).
    False means it is off (closed-lid setup coffee can use). None means the
    saved preference is missing or not a boolean, so the checkbox is not
    confirmed. Absent is not treated as the app's factory default.
    """
    if not preferences or AMPHETAMINE_CLOSED_DISPLAY_SLEEP_KEY not in preferences:
        return None
    value = preferences[AMPHETAMINE_CLOSED_DISPLAY_SLEEP_KEY]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            return True
        if lowered in {"0", "false", "no"}:
            return False
    return None


def read_closed_display_sleep_allowed(paths: Sequence[Path] | None = None) -> bool | None:
    """Read the closed-display Session Defaults checkbox. Never writes it."""
    return parse_closed_display_sleep_allowed(read_amphetamine_preferences(paths))


@dataclass(frozen=True)
class AmphetamineReadiness:
    """Whether the next hold may use Amphetamine, and the line to log if not.

    ``state`` is ``ready``, ``skipped`` (``--pmset``), ``missing``,
    ``not-usable``, or ``not-configured``. Only ``ready`` selects Amphetamine.
    ``reason`` is the backend explanation, including "using pmset instead"
    whenever Amphetamine is not used.
    """

    state: str
    reason: str


def assess_amphetamine(
    *,
    force_pmset: bool,
    installed: bool,
    usable: bool,
    closed_display_sleep_allowed: bool | None,
) -> AmphetamineReadiness:
    """Decide if Amphetamine is ready for a closed-lid hold.

    Order:
    1. ``--pmset`` skips Amphetamine. That path needs no App Store install.
    2. No app bundle → missing. pmset is the hold.
    3. Bundle without a MacOS executable → not usable. pmset is the hold.
    4. Checkbox saved on, or not saved at all → not configured. pmset is the hold.
    5. Checkbox saved off → ready. Amphetamine may start a session.

    The checkbox cannot be flipped from AppleScript. A missing preference is
    not assumed off: coffee only trusts a saved false value.
    """
    if force_pmset:
        return AmphetamineReadiness(
            "skipped",
            "pmset requested (--pmset); zero-setup, Amphetamine is not required",
        )
    if not installed:
        return AmphetamineReadiness(
            "missing",
            "Amphetamine is not installed; using pmset instead",
        )
    if not usable:
        return AmphetamineReadiness(
            "not-usable",
            "Amphetamine is installed but the app bundle cannot be launched; using pmset instead",
        )
    if closed_display_sleep_allowed is None:
        return AmphetamineReadiness(
            "not-configured",
            "Amphetamine is installed but closed-lid Session Defaults are not confirmed off; "
            "using pmset instead",
        )
    if closed_display_sleep_allowed:
        return AmphetamineReadiness(
            "not-configured",
            "Amphetamine still allows system sleep when the display is closed; using pmset instead",
        )
    return AmphetamineReadiness(
        "ready",
        "Amphetamine is installed and Session Defaults disallow sleep when the display is closed",
    )


def choose_backend(readiness: AmphetamineReadiness) -> tuple[str, str]:
    """Return ``(backend, why)``. Amphetamine only when ``readiness`` is ready."""
    if readiness.state == "ready":
        return "amphetamine", readiness.reason
    return "pmset", readiness.reason


def _pmset_switch_line(*, dry_run: bool, until: str) -> str:
    verb = "Would use" if dry_run else "Using"
    return f"{verb} pmset instead (`sudo pmset -a disablesleep 1`) {until}"


def amphetamine_fallback_lines(
    readiness: AmphetamineReadiness,
    *,
    dry_run: bool,
) -> tuple[str, ...]:
    """Setup steps and the pmset switch, for the log and for ``lf coffee status``.

    Empty when Amphetamine is ready or when the user passed ``--pmset``.
    Callers print every line. The switch must stay visible; do not drop it
    when shortening output.
    """
    checkbox = (
        'Setup: uncheck Session Defaults → "Allow system sleep when display is closed" once. '
        "Amphetamine's scripting API cannot change that checkbox, and coffee will not change it."
    )
    zero_setup = "`lf coffee --pmset` is the zero-setup path and does not need Amphetamine."
    if readiness.state == "missing":
        return (
            "Amphetamine is not installed, so coffee cannot use it.",
            "Setup: install Amphetamine from the Mac App Store (free). `lf coffee setup` opens that page.",
            checkbox,
            _pmset_switch_line(
                dry_run=dry_run,
                until="until Amphetamine is installed and that checkbox is off.",
            ),
            zero_setup,
        )
    if readiness.state == "not-configured":
        return (
            "Amphetamine is installed, but it is not configured for a closed lid.",
            checkbox,
            "coffee uses Amphetamine only when that preference is saved off "
            f"(`{AMPHETAMINE_CLOSED_DISPLAY_SLEEP_KEY}` is false). "
            "If the box is already unchecked, toggle it once so Amphetamine saves it. "
            "coffee reads the saved value and will not write it.",
            _pmset_switch_line(dry_run=dry_run, until="until that checkbox is off."),
            "`lf coffee setup` opens Amphetamine. " + zero_setup,
        )
    if readiness.state == "not-usable":
        return (
            "Amphetamine is installed but not usable, so coffee cannot start a session with it.",
            "Setup: reinstall Amphetamine from the Mac App Store if it will not open. "
            "`lf coffee setup` opens that page.",
            checkbox,
            _pmset_switch_line(dry_run=dry_run, until="for this run."),
            zero_setup,
        )
    return ()


def session_failed_readiness() -> AmphetamineReadiness:
    """Readiness after Amphetamine was selected but the session would not start."""
    return AmphetamineReadiness(
        "not-usable",
        "Amphetamine session could not be started; using pmset instead",
    )


@dataclass(frozen=True)
class AmphetamineSetupPlan:
    """One GUI open for ``lf coffee setup`` or the first-run helper.

    ``kind`` is ``mas`` (Mac App Store CLI), ``url`` (public App Store page),
    or ``app`` (Amphetamine itself). ``message`` is a single line for the user.
    Nothing here writes Session Defaults.
    """

    kind: str
    argv: tuple[str, ...]
    message: str


def plan_amphetamine_setup(
    *,
    installed: bool,
    mas_on_path: bool,
    closed_display_sleep_allowed: bool | None = None,
) -> AmphetamineSetupPlan:
    """Choose how to open Amphetamine's install page or the app.

    Not installed: ``mas open`` when the ``mas`` CLI is on PATH, otherwise
    ``open`` of the public App Store URL. Installed: open the app. There is
    no scripting command for Preferences, so the message tells the user which
    checkbox to uncheck, unless that preference is already saved off.
    """
    if not installed:
        if mas_on_path:
            return AmphetamineSetupPlan(
                "mas",
                ("mas", "open", AMPHETAMINE_APP_STORE_ID),
                "Opening the Amphetamine page in the Mac App Store with mas. "
                "Install it, then uncheck Session Defaults → "
                '"Allow system sleep when display is closed" once. '
                "`lf coffee --pmset` needs no Amphetamine.",
            )
        return AmphetamineSetupPlan(
            "url",
            ("open", AMPHETAMINE_APP_STORE_URL),
            "Opening the Amphetamine App Store page. "
            "Install it, then uncheck Session Defaults → "
            '"Allow system sleep when display is closed" once. '
            "`lf coffee --pmset` needs no Amphetamine.",
        )
    if closed_display_sleep_allowed is False:
        return AmphetamineSetupPlan(
            "app",
            ("open", "-a", "Amphetamine"),
            "Opened Amphetamine. Session Defaults already disallow system sleep "
            "when the display is closed. coffee did not change that checkbox.",
        )
    return AmphetamineSetupPlan(
        "app",
        ("open", "-a", "Amphetamine"),
        AMPHETAMINE_SESSION_DEFAULTS_NOTE,
    )


def run_setup_plan(
    plan: AmphetamineSetupPlan,
    run: Run | None = None,
) -> AmphetamineSetupPlan:
    """Open the planned page or app. ``mas`` failure retries the public URL.

    Returns the plan that actually ran so the caller prints that line.
    Does not install Amphetamine and does not write its preferences.
    """
    runner = default_run if run is None else run
    try:
        runner(list(plan.argv), interactive=False, timeout=30)
    except CommandError:
        if plan.kind != "mas":
            raise
        # ``mas`` is installed but could not open the listing (not signed in,
        # or an older mas without ``open``). The public URL is the same page.
        fallback = plan_amphetamine_setup(installed=False, mas_on_path=False)
        runner(list(fallback.argv), interactive=False, timeout=30)
        return fallback
    return plan


def start_session_script() -> str:
    """AppleScript for a display-sleep-friendly Amphetamine session.

    ``displaySleepAllowed:true`` lets the screen go black while the session
    keeps the system awake. Closed-display behavior still depends on
    Amphetamine's Session Defaults checkbox, which this script cannot set.
    """
    return (
        'tell application "Amphetamine"\n'
        f"    start new session with options {{duration:{AMPHETAMINE_SESSION_HOURS}, "
        "interval:hours, displaySleepAllowed:true}}\n"
        "end tell"
    )


def start_session_url() -> str:
    return (
        "amphetamine://start-new-session?"
        f"duration={AMPHETAMINE_SESSION_HOURS}&interval=hours&displaySleepAllowed=true"
    )


def pmset_argv(value: int, *, interactive: bool) -> list[str]:
    argv = ["sudo", "pmset", "-a", "disablesleep", str(int(value))]
    if not interactive:
        argv.insert(1, "-n")
    return argv


@dataclass
class HoldLock:
    pid: int
    backend: str
    previous_disablesleep: int | None
    acquired: bool

    def to_json(self) -> dict[str, object]:
        return {
            "version": 1,
            "pid": self.pid,
            "backend": self.backend,
            "previous_disablesleep": self.previous_disablesleep,
            "acquired": self.acquired,
        }


def read_lock(path: Path) -> HoldLock | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        pid = int(payload["pid"])
        backend = str(payload["backend"])
    except (KeyError, TypeError, ValueError):
        return None
    if backend not in {"pmset", "amphetamine"}:
        return None
    previous = payload.get("previous_disablesleep")
    if previous is not None:
        try:
            previous = int(previous)
        except (TypeError, ValueError):
            previous = 0
    return HoldLock(
        pid=pid,
        backend=backend,
        previous_disablesleep=previous,
        acquired=bool(payload.get("acquired", True)),
    )


def write_lock(path: Path, lock: HoldLock) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(lock.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def clear_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def exclusive_hold(state_dir: Path) -> Iterator[None]:
    """Serialize acquire/restore so a dying supervisor cannot clear a new hold."""
    import fcntl

    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(state_dir / "hold.flock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _end_amphetamine(run: Run) -> None:
    errors: list[str] = []
    for argv in (
        ["osascript", "-e", END_SESSION_SCRIPT],
        ["open", END_SESSION_URL],
    ):
        try:
            run(argv, interactive=False, timeout=20)
            return
        except CommandError as exc:
            errors.append(str(exc))
    raise CommandError(["osascript"], 1, "; ".join(errors))


def _restore_pmset(previous: int, run: Run) -> str:
    current = read_disablesleep(run)
    if current == previous:
        return f"disablesleep already {previous}"
    run(pmset_argv(previous, interactive=False), interactive=False, timeout=30)
    return f"pmset disablesleep {previous}"


def _apply_restore(lock: HoldLock, run: Run) -> str:
    if lock.backend == "pmset":
        previous = 0 if lock.previous_disablesleep is None else lock.previous_disablesleep
        return _restore_pmset(previous, run)
    if lock.backend == "amphetamine":
        _end_amphetamine(run)
        return "Amphetamine session ended"
    return f"unknown backend {lock.backend}"


def recover_orphan_lock(
    state_dir: Path,
    *,
    only_pid: int | None = None,
    run: Run = default_run,
    pid_alive_fn: Callable[[int], bool] = pid_alive,
) -> str | None:
    """Restore sleep if the coffee process that took the hold is gone.

    ``only_pid`` lets the supervisor ignore a lock a newer coffee has replaced.
    The flock is held across the check and the restore so those cannot interleave.
    A failed restore leaves the lock in place for the next status/start/stop.
    """
    with exclusive_hold(state_dir):
        path = state_dir / "hold.lock"
        lock = read_lock(path)
        if lock is None:
            return None
        if only_pid is not None and lock.pid != only_pid:
            return None
        if pid_alive_fn(lock.pid):
            return None
        try:
            detail = _apply_restore(lock, run)
        except CommandError as exc:
            return f"could not restore sleep hold: {exc}"
        current = read_lock(path)
        if current is not None and current.pid == lock.pid:
            clear_lock(path)
        return f"restored leftover sleep hold ({detail})"


def spawn_supervisor(state_dir: Path, parent_pid: int) -> subprocess.Popen[bytes]:
    """Start a detached process that restores the hold after ``parent_pid`` dies.

    The child is in its own session so SIGINT/SIGKILL of the daemon does not
    kill it. ``close_fds`` keeps it from inheriting the daemon singleton lock.
    """
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "tools.coffee_awake", "supervise", str(state_dir), str(parent_pid)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=root,
        close_fds=True,
    )


def supervise(
    state_dir: Path,
    parent_pid: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 1.0,
    grace_seconds: float = 2.0,
    run: Run = default_run,
) -> None:
    """Block until ``parent_pid`` exits, then restore its sleep hold.

    The grace period lets an in-flight ``pmset`` child finish before we write
    the previous ``disablesleep`` value back.
    """
    while pid_alive(parent_pid):
        sleep(interval)
    if grace_seconds > 0:
        sleep(grace_seconds)
    recover_orphan_lock(state_dir, only_pid=parent_pid, run=run)


class PmsetBackend:
    """Own ``disablesleep`` only when this process is the one that turned it on."""

    name = "pmset"

    def __init__(
        self,
        state_dir: Path,
        run: Run = default_run,
        spawn: Callable[[Path, int], object] | None = None,
    ):
        self.state_dir = state_dir
        self.run = run
        self.spawn = spawn or spawn_supervisor
        self.owns_setting = False
        self._supervisor: object | None = None

    def acquire(self) -> str:
        # Read and record the previous value before changing it, but do not
        # hold the flock across the sudo prompt. The supervisor covers death
        # during the prompt; it checks the lock pid so it will not clobber a
        # newer coffee process.
        lock_path = self.state_dir / "hold.lock"
        with exclusive_hold(self.state_dir):
            current = read_disablesleep(self.run)
            if current is None:
                raise CommandError(["pmset", "-g"], 1, "could not read disablesleep")
            if current == 1:
                self.owns_setting = False
                return "disablesleep already 1; leaving it unchanged"
            write_lock(
                lock_path,
                HoldLock(
                    pid=os.getpid(),
                    backend="pmset",
                    previous_disablesleep=current,
                    acquired=True,
                ),
            )
        try:
            self._supervisor = self.spawn(self.state_dir, os.getpid())
            self.run(pmset_argv(1, interactive=True), interactive=True, timeout=120)
        except Exception:
            with exclusive_hold(self.state_dir):
                current_lock = read_lock(lock_path)
                if current_lock is not None and current_lock.pid == os.getpid():
                    clear_lock(lock_path)
            self.owns_setting = False
            raise
        self.owns_setting = True
        return "sudo pmset -a disablesleep 1"

    def release(self) -> str:
        if not self.owns_setting:
            return "disablesleep was already set; left unchanged"
        with exclusive_hold(self.state_dir):
            lock = read_lock(self.state_dir / "hold.lock")
            previous = 0 if lock is None or lock.previous_disablesleep is None else lock.previous_disablesleep
            detail = _restore_pmset(previous, self.run)
            clear_lock(self.state_dir / "hold.lock")
            self.owns_setting = False
            return detail

    def keep_warm(self) -> bool:
        """Refresh the sudo timestamp so a later death can restore without a prompt."""
        if not self.owns_setting:
            return True
        try:
            self.run(["sudo", "-n", "-v"], interactive=False, timeout=10)
            return True
        except CommandError:
            return False


class AmphetamineBackend:
    """Start and end an Amphetamine session. A supervisor ends it if we die."""

    name = "amphetamine"

    def __init__(
        self,
        state_dir: Path,
        run: Run = default_run,
        spawn: Callable[[Path, int], object] | None = None,
    ):
        self.state_dir = state_dir
        self.run = run
        self.spawn = spawn or spawn_supervisor
        self.owns_setting = False
        self._supervisor: object | None = None
        self._started_at = 0.0

    def acquire(self) -> str:
        how = self._start()
        lock_path = self.state_dir / "hold.lock"
        try:
            write_lock(
                lock_path,
                HoldLock(
                    pid=os.getpid(),
                    backend="amphetamine",
                    previous_disablesleep=None,
                    acquired=True,
                ),
            )
            self._supervisor = self.spawn(self.state_dir, os.getpid())
        except Exception:
            try:
                self._end()
            except CommandError:
                pass
            clear_lock(lock_path)
            raise
        self.owns_setting = True
        self._started_at = time.monotonic()
        return (
            f"Amphetamine session via {how}, display sleep allowed, "
            f"{AMPHETAMINE_SESSION_HOURS}h"
        )

    def release(self) -> str:
        if not self.owns_setting:
            return "no Amphetamine session to stop"
        try:
            self._end()
        finally:
            clear_lock(self.state_dir / "hold.lock")
            self.owns_setting = False
        return "Amphetamine session ended"

    def refresh_if_due(self, *, interval: float = 3600) -> None:
        """Restart the session timer before the 12h Amphetamine session expires."""
        if not self.owns_setting:
            return
        if time.monotonic() - self._started_at < interval:
            return
        self._start()
        self._started_at = time.monotonic()

    def _start(self) -> str:
        errors: list[str] = []
        try:
            self.run(["osascript", "-e", start_session_script()], interactive=False, timeout=20)
            return "osascript"
        except CommandError as exc:
            errors.append(str(exc))
        try:
            self.run(["open", start_session_url()], interactive=False, timeout=20)
            return "url"
        except CommandError as exc:
            errors.append(str(exc))
        raise CommandError(["osascript"], 1, "; ".join(errors))

    def _end(self) -> None:
        _end_amphetamine(self.run)


def main(argv: list[str] | None = None) -> int:
    """Supervisor entry: ``python -m tools.coffee_awake supervise STATE_DIR PID``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "supervise":
        state_dir = Path(args[1])
        parent_pid = int(args[2])

        def _stop(signum: int, frame: object) -> None:
            del signum, frame
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        try:
            while pid_alive(parent_pid):
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        # If we were signaled while the parent is still alive, the parent owns
        # the hold and will restore it. Restoring here would clear a live hold.
        if not pid_alive(parent_pid):
            time.sleep(2.0)
            recover_orphan_lock(state_dir, only_pid=parent_pid)
        return 0
    print(
        "usage: python -m tools.coffee_awake supervise STATE_DIR PARENT_PID",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
