"""Hold a Mac awake while a local Cursor agent is working.

``lf coffee`` polls on-disk Cursor state and keeps the machine awake only
during local IDE or cursor-agent work. Cloud agents are ignored. The hold
is released when every local agent is idle, including a plan-mode pause,
and again on exit.

Amphetamine is preferred when it is installed and Session Defaults already
disallow system sleep with the display closed. coffee cannot change that
checkbox. If Amphetamine is missing, not usable, or not configured for a
closed lid, coffee prints the setup steps and uses ``sudo pmset -a
disablesleep 1`` instead, and says so in the log. ``--pmset`` selects that
path up front. The previous ``disablesleep`` value is restored on exit,
on SIGINT/SIGTERM, and if this process dies.

The foreground process prints a short line when the lid, the active agent,
or the sleep hold changes so a closed-lid session can be checked from the
terminal that started it.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import typer

try:
    from ._cli_common import new_typer_app
    from ._cli_output import fatal, info
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_common import new_typer_app
    from tools._cli_output import fatal, info

try:
    from . import coffee_activity as activity
    from . import coffee_awake as awake
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools import coffee_activity as activity
    from tools import coffee_awake as awake


app = new_typer_app()

_LOCK_FD: int | None = None
# After a failed acquire, wait this long before prompting again.
_ACQUIRE_RETRY_SECONDS = 300
# Transient observation errors keep an existing hold this many polls, then release.
_OBSERVATION_FAILURES_BEFORE_RELEASE = 3


@dataclass(frozen=True)
class CoffeeConfig:
    idle_seconds: int
    poll_seconds: int
    force_pmset: bool
    allow_battery: bool
    dry_run: bool
    background: bool
    verbose: bool


class DryRunBackend:
    """Report hold changes without touching Amphetamine or pmset."""

    name = "dry-run"
    owns_setting = False

    def __init__(self, real_name: str):
        self.real_name = real_name

    def acquire(self) -> str:
        return f"dry-run; would use {self.real_name}"

    def release(self) -> str:
        return "dry-run; sleep settings unchanged"

    def keep_warm(self) -> bool:
        return True

    def refresh_if_due(self) -> None:
        return None


class HoldController:
    """Remember whether this process currently believes the hold is on."""

    def __init__(
        self,
        backend: Any,
        *,
        backend_reason: str = "",
        amphetamine_state: str = "",
    ):
        self.backend = backend
        self.held = False
        # Logged on every status read so a pmset fallback is not only in the
        # startup scrollback.
        self.backend_reason = backend_reason
        self.amphetamine_state = amphetamine_state

    def acquire(self) -> str:
        note = self.backend.acquire()
        self.held = True
        return str(note)

    def release(self) -> str:
        if not self.held:
            return ""
        note = self.backend.release()
        self.held = False
        return str(note)


def coffee_state_dir() -> Path:
    return Path.home() / ".config" / "lf-toolbox" / "coffee"


def require_macos() -> None:
    if sys.platform == "darwin":
        return
    fatal(
        "lf coffee keeps a Mac awake while a local Cursor agent is working. "
        "It drives Amphetamine or `pmset disablesleep`, which exist only on macOS. "
        f"This machine is {sys.platform}; sleep settings were not changed."
    )


def emit(text: str, *, now: datetime | None = None) -> None:
    """Print one timestamped status line. Callers should not print every poll."""
    stamp = (now or datetime.now()).strftime("%H:%M:%S")
    info(f"{stamp}  {text}")


def release_line(note: str) -> str:
    cleaned = " ".join(note.split())
    if cleaned:
        return f"sleeping / hold released ({cleaned})"
    return "sleeping / hold released"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _ensure_single_instance(state_dir: Path) -> None:
    """Hold an exclusive flock for the life of the daemon."""
    import fcntl

    global _LOCK_FD
    fd = os.open(state_dir / "daemon.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        fatal("coffee is already running. Use `lf coffee status` or `lf coffee stop`.")
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _LOCK_FD = fd


def daemon_is_running(state_dir: Path) -> bool:
    import fcntl

    path = state_dir / "daemon.lock"
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _read_pid(state_dir: Path) -> int | None:
    lock_path = state_dir / "daemon.lock"
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    if lines:
        try:
            return int(lines[0].strip())
        except ValueError:
            pass
    state = _read_json(state_dir / "state.json")
    pid = state.get("pid") if state else None
    return pid if isinstance(pid, int) else None


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not awake.pid_alive(pid):
            return True
        time.sleep(0.1)
    return not awake.pid_alive(pid)


def _cursor_running() -> bool | None:
    try:
        result = awake.default_run(["ps", "-ax", "-o", "comm="], interactive=False, timeout=3)
    except awake.CommandError:
        return None
    return activity.cursor_running_from_ps(result.stdout)


def _backend_label(backend: Any) -> str:
    real_name = getattr(backend, "real_name", None)
    if isinstance(real_name, str) and real_name:
        return f"dry-run:{real_name}"
    name = getattr(backend, "name", None)
    return str(name) if name else "unknown"


def _state_payload(
    controller: HoldController,
    snapshot: activity.MonitorSnapshot,
    config: CoffeeConfig,
) -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "backend": _backend_label(controller.backend),
        "hold": controller.held,
        "lid": snapshot.lid,
        "lid_detail": snapshot.lid_detail,
        "phase": snapshot.phase.value,
        "reason": snapshot.reason,
        "power": snapshot.power,
        "agents": [
            {
                "key": agent.key,
                "name": agent.name,
                "id": agent.composer_id,
                "workspace": agent.workspace,
                "reason": agent.reason,
            }
            for agent in snapshot.agents
        ],
        "updated_at": time.time(),
        "dry_run": config.dry_run,
        "backend_reason": controller.backend_reason,
        "amphetamine": controller.amphetamine_state,
    }


def _format_saved_agent(payload: dict[str, Any]) -> str:
    agent = activity.BusyAgent(
        key=str(payload.get("key") or payload.get("id") or "agent"),
        name=str(payload.get("name") or payload.get("key") or "agent"),
        composer_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
        workspace=payload.get("workspace") if isinstance(payload.get("workspace"), str) else None,
        reason=str(payload.get("reason") or ""),
    )
    return activity.format_agent(agent)


def _print_saved_state(state_dir: Path, *, running: bool) -> None:
    state = _read_json(state_dir / "state.json")
    if not state:
        return
    prefix = "" if running else "last "
    info(f"{prefix}backend: {state.get('backend', 'unknown')}")
    backend_reason = state.get("backend_reason")
    if isinstance(backend_reason, str) and backend_reason:
        info(f"{prefix}backend detail: {backend_reason}")
    amphetamine_state = state.get("amphetamine")
    if isinstance(amphetamine_state, str) and amphetamine_state:
        info(f"{prefix}amphetamine: {amphetamine_state}")
    info(f"{prefix}hold: {'yes' if state.get('hold') else 'no'}")
    lid = state.get("lid", "unknown")
    detail = state.get("lid_detail") or "best-effort"
    info(f"{prefix}lid: {lid} ({detail})")
    info(f"{prefix}power: {state.get('power', 'unknown')}")
    info(f"{prefix}agent: {state.get('phase', 'unknown')}")
    agents = state.get("agents")
    if isinstance(agents, list) and agents:
        for item in agents:
            if isinstance(item, dict):
                info(f"  {_format_saved_agent(item)}")
    else:
        info("  none")
    reason = state.get("reason")
    if isinstance(reason, str) and reason:
        info(f"{prefix}reason: {reason}")
    updated = state.get("updated_at")
    if isinstance(updated, (int, float)):
        info(f"{prefix}updated: {datetime.fromtimestamp(updated).strftime('%H:%M:%S')}")


def _emit_banner(config: CoffeeConfig, backend_name: str, why: str) -> None:
    emit("coffee: watching local Cursor agent work")
    emit(f"backend: {backend_name} ({why})")
    if config.dry_run:
        emit("dry-run: detection only; sleep settings will not change")
    elif backend_name == "amphetamine":
        emit(
            "Using Amphetamine. The display may sleep. "
            "Session Defaults already disallow system sleep when the display is closed. "
            "coffee did not change that checkbox."
        )
    else:
        emit(
            "pmset disablesleep keeps the Mac awake with the lid closed. "
            "sudo may prompt once. coffee restores disablesleep on exit, "
            "on SIGINT/SIGTERM, and if this process dies."
        )
    emit(
        "detection is heuristic (local transcripts and composer status) and may need "
        f"--idle-seconds tuning. Cloud agents are ignored. "
        f"Idle {config.idle_seconds}s, poll {config.poll_seconds}s. "
        "Lid state is best-effort."
    )


def _build_backend(name: str, state_dir: Path, *, dry_run: bool) -> Any:
    if dry_run:
        return DryRunBackend(name)
    if name == "amphetamine":
        return awake.AmphetamineBackend(state_dir)
    return awake.PmsetBackend(state_dir)


def _observe(config: CoffeeConfig) -> activity.MonitorSnapshot:
    decision = activity.scan_activity(
        now=time.time(),
        idle_seconds=float(config.idle_seconds),
        cursor_running=_cursor_running(),
        config_dir=activity.default_cursor_config_dir(),
        user_dir=activity.default_cursor_user_dir(),
    )
    lid = awake.read_lid_state()
    power = awake.read_power_source()
    transition = activity.transition_hold(
        phase=decision.phase,
        power=power,
        allow_battery=config.allow_battery,
    )
    block = transition.reason if decision.phase is activity.AgentPhase.BUSY and not transition.hold else None
    return activity.MonitorSnapshot(
        lid=lid.position,
        lid_detail=lid.detail,
        agents=decision.agents,
        phase=decision.phase,
        reason=decision.reason,
        power=power,
        want_hold=transition.hold,
        block_reason=block,
        warning=decision.warning,
    )


def _apply_steps(
    steps: list[activity.StatusStep],
    controller: HoldController,
    state_dir: Path,
    *,
    on_acquire_failure: Callable[[], None],
) -> None:
    """Print status lines and change the hold between them.

    A ``release`` step is executed before ``sleeping / hold released`` so the
    final line means the backend has already restored sleep (or reported why
    it could not).
    """
    for step in steps:
        if step.kind == "log":
            emit(step.text)
            continue
        if step.kind == "acquire":
            try:
                note = controller.acquire()
            except awake.CommandError as exc:
                if getattr(controller.backend, "name", "") == "amphetamine":
                    # The session did not start. Say why, print the checkbox
                    # step, then take the pmset hold in this same poll so the
                    # switch is in the log before "hold acquired".
                    failed = awake.session_failed_readiness()
                    controller.backend_reason = f"{failed.reason} ({exc})"
                    controller.amphetamine_state = failed.state
                    for line in awake.amphetamine_fallback_lines(failed, dry_run=False):
                        emit(line)
                    emit(controller.backend_reason)
                    controller.backend = awake.PmsetBackend(state_dir)
                    try:
                        note = controller.acquire()
                    except awake.CommandError as pmset_error:
                        on_acquire_failure()
                        emit(f"could not acquire sleep hold: {pmset_error}")
                        continue
                else:
                    on_acquire_failure()
                    emit(f"could not acquire sleep hold: {exc}")
                    continue
            emit(f"hold acquired ({note})")
            continue
        if step.kind == "release":
            try:
                note = controller.release()
            except awake.CommandError as exc:
                emit(f"could not release sleep hold: {exc}")
                continue
            emit(release_line(note))


def _maintain(controller: HoldController, warned: dict[str, bool]) -> None:
    backend = controller.backend
    if not controller.held:
        return
    refresh = getattr(backend, "refresh_if_due", None)
    if callable(refresh):
        try:
            refresh()
        except awake.CommandError as exc:
            if not warned.get("refresh"):
                emit(f"could not refresh the awake session: {exc}")
                warned["refresh"] = True
    keep_warm = getattr(backend, "keep_warm", None)
    if callable(keep_warm) and backend.__class__ is awake.PmsetBackend:
        if not keep_warm() and not warned.get("sudo"):
            emit(
                "sudo timestamp could not be refreshed; "
                "restore-on-death may fail until the next `lf coffee` command."
            )
            warned["sudo"] = True


def _relaunch_in_background() -> None:
    argv = [arg for arg in sys.argv if arg not in {"--background", "-b"}]
    state_dir = coffee_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "coffee.log"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    handle = open(log_path, "a", encoding="utf-8", buffering=1)
    proc = subprocess.Popen(
        argv,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
    )
    info(f"coffee started in the background (pid {proc.pid}). Log: {log_path}")
    raise typer.Exit()


def _release_for_exit(controller: HoldController) -> None:
    if not controller.held:
        return
    emit("stopping; releasing sleep hold")
    try:
        note = controller.release()
    except awake.CommandError as exc:
        emit(f"could not release sleep hold: {exc}")
        return
    emit(release_line(note))


def _current_readiness(*, force_pmset: bool) -> awake.AmphetamineReadiness:
    """Read the app bundle and the closed-lid preference. Does not write either."""
    installed = awake.amphetamine_installed()
    usable = awake.amphetamine_bundle_usable() if installed else False
    allowed = awake.read_closed_display_sleep_allowed() if installed and usable else None
    return awake.assess_amphetamine(
        force_pmset=force_pmset,
        installed=installed,
        usable=usable,
        closed_display_sleep_allowed=allowed,
    )


def _print_live_readiness(readiness: awake.AmphetamineReadiness) -> None:
    """Print what a plain ``lf coffee`` would do right now."""
    backend_name, why = awake.choose_backend(readiness)
    info(f"amphetamine: {readiness.state}")
    info(f"default backend: {backend_name} ({why})")
    for line in awake.amphetamine_fallback_lines(readiness, dry_run=False):
        info(line)
    if readiness.state == "ready":
        info(
            "Session Defaults already disallow system sleep when the display is closed. "
            "coffee does not change that checkbox. `lf coffee --pmset` skips Amphetamine."
        )


def _maybe_open_setup_helper(
    *,
    readiness: awake.AmphetamineReadiness,
    state_dir: Path,
    dry_run: bool,
    interactive: bool,
    mas_on_path: bool,
    run: awake.Run | None = None,
) -> str | None:
    """Open the App Store page or Amphetamine once for this unreadiness state.

    Instructions are printed on every start even when this returns None.
    Dry-run and non-interactive sessions never open a window. A failed open
    leaves the marker unset so the next foreground start can retry.
    """
    if dry_run or not interactive or readiness.state not in {"missing", "not-configured", "not-usable"}:
        return None
    marker = state_dir / f"setup-opened-{readiness.state}"
    if marker.exists():
        return None
    # Not configured: open the app so Session Defaults is one menu away.
    # Missing or not usable: open the store page (mas, else the public URL).
    plan = awake.plan_amphetamine_setup(
        installed=readiness.state == "not-configured",
        mas_on_path=mas_on_path,
        closed_display_sleep_allowed=None,
    )
    used = awake.run_setup_plan(plan, run=run)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("opened\n", encoding="utf-8")
    return used.message


def serve(config: CoffeeConfig) -> None:
    """Poll local Cursor activity and hold sleep only while it is busy.

    Each iteration reads the lid, power source, and local agent signals, then
    prints a line only when something changed. The order when work finishes is:
    one ``agent finished`` line per agent, ``all local agents idle; releasing
    sleep hold``, the backend restore, then ``sleeping / hold released``.

    Observation errors do not immediately drop a hold that is already on.
    Three failed polls in a row release it, so a stuck reader cannot pin the
    Mac awake. A failed acquire is retried after a few minutes, not every poll.
    """
    require_macos()
    state_dir = coffee_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    # Say which backend this run will use before detaching. A `--background`
    # parent still has the terminal; the child repeats the same lines in the log.
    # Amphetamine is used only when the closed-lid checkbox is already saved off.
    readiness = _current_readiness(force_pmset=config.force_pmset)
    for line in awake.amphetamine_fallback_lines(readiness, dry_run=config.dry_run):
        emit(line)
    if readiness.state in {"missing", "not-configured", "not-usable"}:
        try:
            opened = _maybe_open_setup_helper(
                readiness=readiness,
                state_dir=state_dir,
                dry_run=config.dry_run,
                interactive=sys.stdin.isatty() and sys.stdout.isatty(),
                mas_on_path=shutil.which("mas") is not None,
            )
        except awake.CommandError as exc:
            emit(f"could not open the Amphetamine setup helper: {exc}")
        else:
            if opened:
                emit(opened)
    backend_name, why = awake.choose_backend(readiness)
    _emit_banner(config, backend_name, why)

    if config.background:
        _relaunch_in_background()
        return

    _ensure_single_instance(state_dir)

    restored = awake.recover_orphan_lock(state_dir)
    if restored:
        emit(restored)

    controller = HoldController(
        _build_backend(backend_name, state_dir, dry_run=config.dry_run),
        backend_reason=why,
        amphetamine_state=readiness.state,
    )

    def _safety_release() -> None:
        if controller.held:
            try:
                controller.release()
            except awake.CommandError:
                return

    atexit.register(_safety_release)

    def _request_stop(signum: int, frame: object) -> None:
        del signum, frame
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    previous: activity.MonitorSnapshot | None = None
    acquire_retry_at = float("inf")
    observation_failures = 0
    warned = {"refresh": False, "sudo": False}
    last_maintenance = time.monotonic()
    state_path = state_dir / "state.json"

    def _note_acquire_failure() -> None:
        nonlocal acquire_retry_at
        acquire_retry_at = time.monotonic() + _ACQUIRE_RETRY_SECONDS

    try:
        while True:
            try:
                snapshot = _observe(config)
                observation_failures = 0
            except Exception as exc:
                # Keep the previous busy set across a short I/O blip so one
                # failed ioreg/pmset/sqlite read does not sleep the Mac under
                # an agent. Repeated failure releases instead of holding forever.
                observation_failures += 1
                warning = f"observation failed ({exc})"
                if (
                    observation_failures < _OBSERVATION_FAILURES_BEFORE_RELEASE
                    and previous is not None
                    and controller.held
                ):
                    snapshot = replace(previous, warning=warning)
                else:
                    snapshot = activity.MonitorSnapshot(
                        lid="unknown",
                        lid_detail="best-effort; observation failed",
                        agents=(),
                        phase=activity.AgentPhase.IDLE,
                        reason=warning,
                        power="unknown",
                        want_hold=False,
                        block_reason=None,
                        warning=warning,
                    )

            steps = activity.plan_status_events(
                previous,
                snapshot,
                hold=controller.held,
                verbose=config.verbose,
            )
            if (
                snapshot.want_hold
                and not controller.held
                and time.monotonic() >= acquire_retry_at
                and not any(step.kind == "acquire" for step in steps)
            ):
                steps.append(activity.StatusStep("acquire", ""))
            _apply_steps(steps, controller, state_dir, on_acquire_failure=_note_acquire_failure)
            if controller.held:
                acquire_retry_at = float("inf")
            previous = snapshot
            try:
                _write_json(state_path, _state_payload(controller, snapshot, config))
            except OSError as exc:
                emit(f"could not write status file: {exc}")
            if time.monotonic() - last_maintenance >= 60:
                _maintain(controller, warned)
                last_maintenance = time.monotonic()
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        # Ctrl+C and SIGTERM land here. Release only when this process still
        # owns the hold; an idle session just says it is stopping.
        if controller.held:
            _release_for_exit(controller)
        else:
            emit("stopping")
        if previous is not None:
            try:
                _write_json(state_path, _state_payload(controller, previous, config))
            except OSError:
                pass


def _status() -> None:
    require_macos()
    _print_live_readiness(_current_readiness(force_pmset=False))
    state_dir = coffee_state_dir()
    running = state_dir.exists() and daemon_is_running(state_dir)
    if not running:
        note = awake.recover_orphan_lock(state_dir) if state_dir.exists() else None
        info("running: no")
        if note:
            info(note)
        if state_dir.exists():
            _print_saved_state(state_dir, running=False)
        return
    pid = _read_pid(state_dir)
    info("running: yes")
    if pid is not None:
        info(f"pid: {pid}")
    _print_saved_state(state_dir, running=True)


def _stop() -> None:
    require_macos()
    state_dir = coffee_state_dir()
    if not state_dir.exists() or not daemon_is_running(state_dir):
        note = awake.recover_orphan_lock(state_dir) if state_dir.exists() else None
        info("coffee is not running.")
        if note:
            info(note)
        return
    pid = _read_pid(state_dir)
    if pid is None or not awake.pid_alive(pid):
        note = awake.recover_orphan_lock(state_dir)
        info("coffee is not running.")
        if note:
            info(note)
        return
    os.kill(pid, signal.SIGTERM)
    if not _wait_dead(pid, 5):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _wait_dead(pid, 2)
    info(f"stopped coffee (pid {pid}).")
    note = awake.recover_orphan_lock(state_dir)
    if note:
        info(note)


@app.callback(invoke_without_command=True)
def coffee(
    ctx: typer.Context,
    idle_seconds: int = typer.Option(
        activity.DEFAULT_IDLE_SECONDS,
        "--idle-seconds",
        help="Release the hold after this many seconds without local agent output.",
    ),
    poll_seconds: int = typer.Option(
        activity.DEFAULT_POLL_SECONDS,
        "--poll-seconds",
        help="How often to re-check Cursor and the lid.",
    ),
    pmset: bool = typer.Option(
        False,
        "--pmset",
        help="Use sudo pmset disablesleep instead of Amphetamine.",
    ),
    allow_battery: bool = typer.Option(
        False,
        "--allow-battery",
        help="Hold sleep on battery too. The default requires AC power.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print detection and hold decisions without changing sleep settings.",
    ),
    background: bool = typer.Option(
        False,
        "-b",
        "--background",
        help="Detach and write status lines to the coffee log. Foreground is the default.",
    ),
    verbose: bool = typer.Option(
        False,
        "-V",
        "--verbose",
        help="Print a line on every poll, not only when lid, agent, or hold state changes.",
    ),
) -> None:
    """Hold this Mac awake while a local Cursor agent is working.

    Runs in the foreground and prints a timestamped line when the lid, the
    active agent, or the sleep hold changes. Cloud agents do not keep the
    hold. When every local agent is idle, coffee releases the hold so the
    Mac may sleep. Ctrl+C restores sleep settings before exiting.
    """
    if ctx.invoked_subcommand is not None:
        return
    if idle_seconds < 15:
        raise typer.BadParameter("--idle-seconds must be at least 15")
    if poll_seconds < 1:
        raise typer.BadParameter("--poll-seconds must be at least 1")
    serve(
        CoffeeConfig(
            idle_seconds=idle_seconds,
            poll_seconds=poll_seconds,
            force_pmset=pmset,
            allow_battery=allow_battery,
            dry_run=dry_run,
            background=background,
            verbose=verbose,
        )
    )


@app.command("setup")
def setup() -> None:
    """Open the Amphetamine App Store page, or the app, and print the closed-lid step.

    ``--pmset`` does not need this. coffee cannot uncheck Session Defaults;
    this only opens the page or the app and tells you which box to change.
    """
    require_macos()
    installed = awake.amphetamine_installed()
    usable = awake.amphetamine_bundle_usable() if installed else False
    allowed = awake.read_closed_display_sleep_allowed() if installed and usable else None
    readiness = awake.assess_amphetamine(
        force_pmset=False,
        installed=installed,
        usable=usable,
        closed_display_sleep_allowed=allowed,
    )
    for line in awake.amphetamine_fallback_lines(readiness, dry_run=False):
        info(line)
    if readiness.state == "ready":
        info(
            "Amphetamine is already set up for a closed lid. "
            "coffee did not change Session Defaults. "
            "`lf coffee --pmset` remains the zero-setup path."
        )
    # A broken bundle should get the store page, not `open -a` on a dead app.
    # A saved-off checkbox still opens Amphetamine so the setting can be checked.
    open_app = readiness.state in {"not-configured", "ready"}
    plan = awake.plan_amphetamine_setup(
        installed=open_app,
        mas_on_path=shutil.which("mas") is not None,
        closed_display_sleep_allowed=allowed if open_app else None,
    )
    try:
        used = awake.run_setup_plan(plan)
    except awake.CommandError as exc:
        fatal(f"could not open the Amphetamine setup helper: {exc}")
    info(used.message)


@app.command("status")
def status() -> None:
    """Show Amphetamine readiness, the backend, the lid, and the last agent state."""
    _status()


@app.command("stop")
def stop() -> None:
    """Stop the daemon and restore sleep settings."""
    _stop()


if __name__ == "__main__":
    app()
