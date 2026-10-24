"""macOS sleep holds for ``lf coffee``.

Amphetamine is preferred. A session is started with display sleep allowed so
the panel can go black. Amphetamine's AppleScript dictionary only accepts
duration, interval, and ``displaySleepAllowed`` on ``start new session``.
Staying awake with the lid shut also requires the Session Defaults checkbox
"Allow system sleep when display is closed" to be unchecked. That checkbox
is not a start-session parameter.

``--pmset`` (and the automatic fallback when Amphetamine is missing) runs
``sudo pmset -a disablesleep 1``. Plain ``caffeinate`` is intentionally not
used: its assertions are dropped when the lid closes.

``disablesleep`` is restored on normal release, and if this process dies, by
a detached supervisor plus the lock file. The next ``lf coffee`` / ``status``
/ ``stop`` repairs a lock whose owner is already gone. A hard reboot that
kills both processes is repaired the same way the next time coffee runs.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


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


def read_lid_state(run: Run = default_run) -> LidState:
    """Read the lid via ioreg. Failures become ``unknown`` with a best-effort note."""
    for argv in (
        ["ioreg", "-c", "IOPMrootDomain", "-d", "2"],
        ["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
    ):
        try:
            result = run(argv, interactive=False, timeout=3)
        except CommandError:
            continue
        position, token = parse_clamshell_state(result.stdout)
        if position != "unknown" and token is not None:
            return LidState(position, f"best-effort; AppleClamshellState={token}")
    return LidState("unknown", "best-effort; AppleClamshellState not available")


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


def choose_backend(*, force_pmset: bool, amphetamine_present: bool) -> tuple[str, str]:
    """Return ``(backend, why)``. Amphetamine wins unless ``--pmset`` is set."""
    if force_pmset:
        return "pmset", "pmset requested (--pmset)"
    if amphetamine_present:
        return "amphetamine", "Amphetamine is installed"
    return "pmset", "Amphetamine is not installed; falling back to pmset"


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
