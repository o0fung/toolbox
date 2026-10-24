"""Heuristic detection of local Cursor agent activity.

There is no official Cursor API for "Composer is generating". This module
reads on-disk state that current Cursor releases actually write:

- ``~/.cursor/projects/<project>/agent-transcripts/**/*.jsonl``
  Local IDE and ``cursor-agent`` transcripts, including ``subagents/``.
- ``~/.cursor/chats/<workspace-hash>/<chat-id>/store.db``
  Local cursor-agent session store. ``meta.json`` beside it may hold ``cwd``.
- ``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``
  ``ItemTable`` key ``composer.composerHeaders`` and ``cursorDiskKV`` keys
  ``composerData:<id>``. ``status == "generating"`` means a turn is in progress.
  ``unifiedMode`` is ``agent``, ``chat``, ``edit``, or ``plan``.

Cloud / background agents are ignored. Their rows are tagged with ``bcId``,
a background ``unifiedMode``, or similar fields, and ``backgroundComposer``
state is not treated as local work. Those runs do not need this Mac awake.

Limitations: a long silent tool call can look idle once both the transcript
mtime and composer ``lastUpdatedAt`` go quiet, and a ``generating`` status
that never updates again is ignored after a staleness window. ``--idle-seconds``
is the knob when a hold releases early. Wait phrases in assistant text can
false-trigger on quoted text; only the tail of the newest records is checked.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlparse


DEFAULT_IDLE_SECONDS = 120
DEFAULT_POLL_SECONDS = 20
# A composer left at "generating" with no writes is treated as stuck, not busy.
STUCK_GENERATING_SECONDS = 30 * 60
MAX_COMPOSER_FETCHES = 12
_TAIL_BYTES = 65_536
_WAIT_TEXT_CHARS = 800

# Statuses observed on composerData while a local turn is still in progress.
# Anything else (none, completed, aborted, ...) is not a hold by itself.
_BUSY_STATUSES = frozenset({"generating", "applying", "running", "in_progress"})
_BACKGROUND_MODES = frozenset({"background", "cloud", "background-agent"})
_BACKGROUND_KEYS = (
    "bcId",
    "backgroundComposerId",
    "isBackgroundAgent",
    "isBackgroundComposer",
    "createdFromBackgroundAgent",
)
_WAIT_PHRASES = (
    "waiting for user",
    "waiting for you",
    "waiting on you",
    "waiting on the user",
    "wait for the user",
    "waiting for approval",
    "waiting for your approval",
    "waiting for you to approve",
    "waiting for user to execute",
    "waiting for you to execute",
    "for you to execute the plan",
    "execute this plan",
    "execute the plan",
    "approve this plan",
    "approve the plan",
    "review the plan",
    "plan mode",
)
_WAIT_TOOLS = frozenset({"askquestion", "createplan"})
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_UUID_LIKE_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")


class AgentPhase(str, Enum):
    BUSY = "busy"
    IDLE = "idle"


@dataclass(frozen=True)
class BusyAgent:
    """One local agent that currently counts as in-progress work."""

    key: str
    name: str
    composer_id: str | None
    workspace: str | None
    reason: str


@dataclass(frozen=True)
class TranscriptEvent:
    kind: str
    text: str
    tool_name: str | None


@dataclass(frozen=True)
class TranscriptSignal:
    path: str
    mtime: float
    session_id: str
    label: str
    workspace: str | None
    immediate_idle: bool
    marker: str | None


@dataclass(frozen=True)
class SessionInfo:
    composer_id: str
    name: str
    workspace: str | None
    is_background: bool
    last_updated_at: float
    unified_mode: str | None


@dataclass(frozen=True)
class ComposerSnapshot:
    composer_id: str
    name: str
    status: str
    unified_mode: str | None
    last_updated_at: float
    workspace: str | None
    is_background: bool
    immediate_idle: bool
    marker: str | None


@dataclass(frozen=True)
class ActivityDecision:
    phase: AgentPhase
    reason: str
    source: str
    evidence: str | None
    agents: tuple[BusyAgent, ...]
    warning: str | None = None

    @property
    def busy(self) -> bool:
        return self.phase is AgentPhase.BUSY


@dataclass(frozen=True)
class HoldTransition:
    hold: bool
    reason: str


@dataclass(frozen=True)
class MonitorSnapshot:
    """One poll of lid + agent + power, as shown in the terminal."""

    lid: str
    lid_detail: str
    agents: tuple[BusyAgent, ...]
    phase: AgentPhase
    reason: str
    power: str
    want_hold: bool
    block_reason: str | None
    warning: str | None = None


@dataclass(frozen=True)
class StatusStep:
    """A terminal line, or a hold change the daemon must perform between lines.

    ``kind`` is ``log``, ``acquire``, or ``release``. The release line itself
    is printed by the daemon after the backend restores sleep, so a ``release``
    step has empty text.
    """

    kind: str
    text: str


def default_cursor_config_dir() -> Path:
    """Return ``${CURSOR_CONFIG_DIR:-~/.cursor}``."""
    override = os.environ.get("CURSOR_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cursor"


def default_cursor_user_dir() -> Path:
    """Return the Cursor User directory for this platform.

    macOS: ``~/Library/Application Support/Cursor/User``
    Linux: ``$XDG_CONFIG_HOME/Cursor/User`` or ``~/.config/Cursor/User``
    Windows: ``%APPDATA%/Cursor/User``
    """
    if os.name == "nt":
        root = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(root) / "Cursor" / "User"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "Cursor" / "User"


def cursor_running_from_ps(text: str) -> bool:
    """Return whether ``ps`` output includes the Cursor app or cursor-agent.

    Helper processes (``Cursor Helper``) count: they are children of the IDE.
    A missing process list is handled by the caller, not here.
    """
    for line in text.splitlines():
        name = line.strip()
        if not name:
            continue
        base = name.rsplit("/", 1)[-1]
        if base in {"Cursor", "cursor-agent"} or base.startswith("cursor-agent"):
            return True
        if base.startswith("Cursor Helper"):
            return True
        if "Cursor.app/Contents/MacOS/Cursor" in name:
            return True
    return False


def composer_is_background(data: dict) -> bool:
    """Return whether a composer row is a cloud/background agent.

    Local IDE and cursor-agent sessions are the only ones that should keep
    the Mac awake. Background composers run off-box; a ``bcId`` (or a
    background unifiedMode) is the marker Cursor stores on those rows.
    """
    mode = data.get("unifiedMode")
    if isinstance(mode, str) and mode.strip().lower() in _BACKGROUND_MODES:
        return True
    for key in _BACKGROUND_KEYS:
        value = data.get(key)
        if value not in (None, False, "", 0):
            return True
    composer_id = data.get("composerId")
    if isinstance(composer_id, str) and composer_id.startswith("bc-"):
        return True
    return False


def _text_is_wait(text: str) -> bool:
    if not text:
        return False
    sample = text[-_WAIT_TEXT_CHARS:].lower()
    return any(phrase in sample for phrase in _WAIT_PHRASES)


def _tool_is_wait(name: str | None, payload: str) -> bool:
    if not name:
        return False
    normalized = name.strip().lower()
    if normalized in _WAIT_TOOLS:
        return True
    # SwitchMode continues unless it is the handoff into plan review.
    return normalized == "switchmode" and "plan" in payload.lower()


def _extract_text(obj: dict) -> str:
    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    content = message.get("content") if isinstance(message, dict) else None
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    if isinstance(message, dict) and isinstance(message.get("text"), str):
        parts.append(message["text"])
    if isinstance(obj.get("text"), str):
        parts.append(obj["text"])
    return "\n".join(part for part in parts if part.strip())


def _assistant_events(obj: dict) -> list[TranscriptEvent]:
    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    content = message.get("content") if isinstance(message, dict) else None
    events: list[TranscriptEvent] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").lower()
            if part_type in {"tool_use", "tool_call"}:
                name = part.get("name") or part.get("toolName")
                payload = json.dumps(part.get("input") or part.get("args") or {}, default=str)[:2000]
                events.append(TranscriptEvent("tool", payload, str(name) if name else None))
            elif isinstance(part.get("text"), str) and part["text"].strip():
                events.append(TranscriptEvent("assistant", part["text"], None))
    elif isinstance(content, str) and content.strip():
        events.append(TranscriptEvent("assistant", content, None))
    if not events:
        text = _extract_text(obj)
        if text.strip():
            events.append(TranscriptEvent("assistant", text, None))
    tool_data = obj.get("toolFormerData")
    if isinstance(tool_data, dict):
        name = tool_data.get("name") or tool_data.get("toolName")
        if name:
            payload = json.dumps(tool_data, default=str)[:2000]
            events.append(TranscriptEvent("tool", payload, str(name)))
    return events


def _events_from_object(obj: dict) -> list[TranscriptEvent]:
    role = str(obj.get("role") or obj.get("kind") or "").lower()
    type_name = obj.get("type")
    type_text = str(type_name).lower() if isinstance(type_name, str) else ""
    if role in {"turn_ended", "turn-ended"} or type_text in {"turn_ended", "turn-ended"}:
        return [TranscriptEvent("turn_ended", "", None)]
    if type_name == 1 or role == "user":
        return [TranscriptEvent("user", _extract_text(obj), None)]
    if type_name == 2 or role == "assistant":
        return _assistant_events(obj)
    if role in {"tool_use", "tool"} or type_text in {"tool_use", "tool_call"}:
        name = obj.get("name") or obj.get("toolName") or obj.get("tool_name")
        payload = json.dumps(obj.get("input") or obj.get("args") or {}, default=str)[:2000]
        return [TranscriptEvent("tool", payload, str(name) if name else None)]
    return []


def classify_events(events: Sequence[TranscriptEvent]) -> tuple[str, str | None]:
    """Classify the tail of one transcript.

    ``waiting`` means the agent stopped for the user (plan approval, a
    question, or an explicit wait phrase) and the hold should drop now.
    A bare ``turn_ended`` is not that signal: agent loops can write it
    between steps, and treating it as idle would sleep the Mac mid-task.
    Other tails stay active and follow the idle timeout via file mtime.
    """
    trimmed = [event for event in events if event.kind != "turn_ended"]
    if not trimmed:
        return ("unknown", "turn_ended") if events else ("unknown", None)
    last = trimmed[-1]
    if last.kind == "tool" and _tool_is_wait(last.tool_name, last.text):
        return "waiting", last.tool_name or "wait-tool"
    if last.kind == "assistant" and _text_is_wait(last.text):
        return "waiting", "wait-phrase"
    if last.kind == "user":
        return "active", "user-turn"
    if last.kind == "tool":
        return "active", last.tool_name or "tool"
    if last.kind == "assistant":
        return "active", "assistant"
    return "unknown", None


def classify_transcript_text(text: str) -> tuple[str, str | None]:
    events: list[TranscriptEvent] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.extend(_events_from_object(obj))
    return classify_events(events[-40:])


def read_tail(path: Path, max_bytes: int = _TAIL_BYTES) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _as_epoch_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # composer timestamps are unix milliseconds. Small values are already seconds.
    if number > 10_000_000_000:
        return number / 1000.0
    return number


def _clean_workspace(value: str) -> str:
    text = value.strip()
    if text.startswith("file://"):
        return unquote(urlparse(text).path)
    return text


def _workspace_from_mapping(data: dict) -> str | None:
    ident = data.get("workspaceIdentifier")
    if isinstance(ident, dict):
        uri = ident.get("uri")
        if isinstance(uri, dict):
            for key in ("fsPath", "path", "external"):
                raw = uri.get(key)
                if isinstance(raw, str) and raw.strip():
                    return _clean_workspace(raw)
        for key in ("fsPath", "path"):
            raw = ident.get(key)
            if isinstance(raw, str) and raw.strip():
                return _clean_workspace(raw)
    for key in ("workspaceProjectDir", "cwd"):
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    uris = data.get("workspaceUris")
    if isinstance(uris, list):
        for item in uris:
            if isinstance(item, str) and item.strip():
                return _clean_workspace(item)
    return None


def _session_name(data: dict, fallback: str) -> str:
    for key in ("name", "subtitle"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return fallback


def _decode_json(value: object) -> object | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _session_info_from_header(item: dict) -> SessionInfo | None:
    composer_id = item.get("composerId")
    if not isinstance(composer_id, str) or not composer_id.strip():
        return None
    mode = item.get("unifiedMode")
    mode_s = mode.strip().lower() if isinstance(mode, str) and mode.strip() else None
    return SessionInfo(
        composer_id=composer_id,
        name=_session_name(item, composer_id),
        workspace=_workspace_from_mapping(item),
        is_background=composer_is_background(item),
        last_updated_at=_as_epoch_seconds(item.get("lastUpdatedAt")) or 0.0,
        unified_mode=mode_s,
    )


def _read_headers(conn: sqlite3.Connection) -> list[dict]:
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            ("composer.composerHeaders",),
        ).fetchone()
    except sqlite3.Error:
        return []
    if not row:
        return []
    payload = _decode_json(row[0])
    if isinstance(payload, dict):
        items = payload.get("allComposers")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _read_composer_blob(conn: sqlite3.Connection, composer_id: str) -> dict | None:
    if not _ID_RE.fullmatch(composer_id):
        return None
    try:
        row = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    payload = _decode_json(row[0])
    return payload if isinstance(payload, dict) else None


def _snapshot_from_composer(data: dict, info: SessionInfo) -> ComposerSnapshot | None:
    raw_id = data.get("composerId")
    composer_id = raw_id if isinstance(raw_id, str) and raw_id else info.composer_id
    status = str(data.get("status") or "none").strip().lower() or "none"
    mode = data.get("unifiedMode")
    mode_s = mode.strip().lower() if isinstance(mode, str) and mode.strip() else info.unified_mode
    updated = _as_epoch_seconds(data.get("lastUpdatedAt")) or info.last_updated_at
    name = _session_name(data, info.name)
    workspace = _workspace_from_mapping(data) or info.workspace
    background = composer_is_background(data) or info.is_background
    subtitle = data.get("subtitle") if isinstance(data.get("subtitle"), str) else ""
    immediate = False
    marker: str | None = None
    # Plan mode that is not generating is the pause before the user approves.
    # A generating plan is still work and must keep the hold.
    if status not in _BUSY_STATUSES:
        if _text_is_wait(subtitle):
            immediate = True
            marker = "subtitle"
        if mode_s == "plan":
            immediate = True
            marker = marker or "plan-mode"
    return ComposerSnapshot(
        composer_id=composer_id,
        name=name,
        status=status,
        unified_mode=mode_s,
        last_updated_at=updated,
        workspace=workspace,
        is_background=background,
        immediate_idle=immediate,
        marker=marker,
    )


def load_cursor_sessions(
    user_dir: Path,
    now: float,
    stuck_seconds: float,
) -> tuple[dict[str, SessionInfo], list[ComposerSnapshot], str | None]:
    """Load composer headers and recent composerData rows.

    Headers name every session (including workspace path on Cursor 3+).
    Only the most recently touched rows are fetched from ``composerData``,
    because those blobs are large and status is only useful while fresh.
    Returns ``(sessions, snapshots, warning)``. A missing database is not
    a warning; transcripts can still prove activity.
    """
    db_path = user_dir / "globalStorage" / "state.vscdb"
    if not db_path.is_file():
        return {}, [], None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.3)
    except sqlite3.Error as exc:
        return {}, [], f"could not open state.vscdb ({exc})"
    try:
        sessions: dict[str, SessionInfo] = {}
        for item in _read_headers(conn):
            info = _session_info_from_header(item)
            if info is not None:
                sessions[info.composer_id] = info
        # Prefer sessions touched inside the stuck window. If headers have no
        # timestamps, still read a bounded set so a fresh composerData status
        # is not missed; decide_activity applies the staleness check on the blob.
        recent = [info for info in sessions.values() if info.last_updated_at >= now - stuck_seconds]
        if not recent:
            recent = list(sessions.values())
        recent.sort(key=lambda info: info.last_updated_at, reverse=True)
        composers: list[ComposerSnapshot] = []
        for info in recent[:MAX_COMPOSER_FETCHES]:
            blob = _read_composer_blob(conn, info.composer_id)
            if blob is None:
                continue
            snapshot = _snapshot_from_composer(blob, info)
            if snapshot is not None:
                composers.append(snapshot)
        return sessions, composers, None
    except sqlite3.Error as exc:
        return {}, [], f"could not read state.vscdb ({exc})"
    finally:
        conn.close()


def _transcript_identity(path: Path) -> tuple[str, str]:
    """Return ``(session_id, label)`` for a transcript or ``store.db`` path."""
    if path.name == "store.db":
        return path.parent.name, path.parent.name
    if "subagents" in path.parts:
        return path.stem, f"subagent {path.stem}"
    return path.stem, path.stem


def _read_meta_cwd(store_db: Path) -> str | None:
    meta_path = store_db.with_name("meta.json")
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("cwd"), str) and payload["cwd"].strip():
        return payload["cwd"].strip()
    return None


def _signal_for_file(
    path: Path,
    kind: str,
    cutoff: float,
    sessions: dict[str, SessionInfo],
    background_ids: set[str],
) -> TranscriptSignal | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if mtime < cutoff:
        return None
    session_id, label = _transcript_identity(path)
    if session_id in background_ids:
        return None
    workspace: str | None = None
    immediate = False
    marker: str | None
    if kind == "store":
        workspace = _read_meta_cwd(path)
        marker = "store.db"
    else:
        try:
            verdict, marker = classify_transcript_text(read_tail(path))
        except OSError:
            verdict, marker = "unknown", None
        immediate = verdict == "waiting"
    info = sessions.get(session_id)
    if info is not None and not info.is_background:
        if info.workspace and not workspace:
            workspace = info.workspace
        if info.name and info.name != session_id:
            label = info.name
    return TranscriptSignal(
        path=str(path),
        mtime=mtime,
        session_id=session_id,
        label=label,
        workspace=workspace,
        immediate_idle=immediate,
        marker=marker,
    )


def collect_transcripts(
    config_dir: Path,
    now: float,
    idle_seconds: float,
    sessions: dict[str, SessionInfo],
    background_ids: set[str],
) -> list[TranscriptSignal]:
    """Stat local transcript files and classify only the fresh ones.

    Older files cannot change the hold, so their contents are not read.
    """
    cutoff = now - idle_seconds
    signals: list[TranscriptSignal] = []
    projects = config_dir / "projects"
    if projects.is_dir():
        for path in projects.glob("*/agent-transcripts/**/*.jsonl"):
            if not path.is_file():
                continue
            signal = _signal_for_file(path, "jsonl", cutoff, sessions, background_ids)
            if signal is not None:
                signals.append(signal)
    chats = config_dir / "chats"
    if chats.is_dir():
        for path in chats.glob("*/*/store.db"):
            if not path.is_file():
                continue
            signal = _signal_for_file(path, "store", cutoff, sessions, background_ids)
            if signal is not None:
                signals.append(signal)
    return signals


def _looks_like_session_id(value: str) -> bool:
    return bool(_UUID_LIKE_RE.fullmatch(value))


def _idle(reason: str, source: str, warning: str | None) -> ActivityDecision:
    return ActivityDecision(
        phase=AgentPhase.IDLE,
        reason=reason,
        source=source,
        evidence=None,
        agents=(),
        warning=warning,
    )


def decide_activity(
    *,
    now: float,
    idle_seconds: float,
    stuck_seconds: float,
    cursor_running: bool | None,
    transcripts: Sequence[TranscriptSignal],
    composers: Sequence[ComposerSnapshot],
    sessions: dict[str, SessionInfo],
    warning: str | None = None,
) -> ActivityDecision:
    """Decide whether any local agent is mid-task.

    Flow:
    1. If Cursor and cursor-agent are confirmed not running, idle immediately.
       ``None`` means the process list could not be read; file signals still count.
    2. Drop background/cloud composers. They must not hold this Mac awake.
    3. A local composer with a busy status and a fresh ``lastUpdatedAt`` is
       busy even when the transcript is quiet (long tool call). A status that
       stays ``generating`` with no writes for ``stuck_seconds`` is ignored.
    4. A transcript or ``store.db`` inside the idle window is busy unless its
       tail is an explicit user wait. Waits release immediately so plan mode
       does not keep the lid awake for the whole timeout. The same session id
       is not listed twice when both the composer and the transcript are hot.
    5. Otherwise idle.

    The busy-agent list is what the terminal prints (name, id, workspace).
    """
    if cursor_running is False:
        return _idle("Cursor is not running", "process", warning)

    found: dict[str, BusyAgent] = {}
    for composer in composers:
        if composer.is_background:
            continue
        age = now - composer.last_updated_at
        if composer.status in _BUSY_STATUSES and -5 <= age <= stuck_seconds:
            found[composer.composer_id] = BusyAgent(
                key=composer.composer_id,
                name=composer.name,
                composer_id=composer.composer_id,
                workspace=composer.workspace,
                reason=f"status {composer.status}",
            )

    waiting_markers: list[str] = []
    for signal in transcripts:
        if signal.mtime < now - idle_seconds:
            continue
        if signal.session_id in found:
            continue
        if signal.immediate_idle:
            waiting_markers.append(signal.marker or "waiting")
            continue
        info = sessions.get(signal.session_id)
        header = info if info is not None and not info.is_background else None
        name = header.name if header is not None else signal.label
        workspace = signal.workspace or (header.workspace if header is not None else None)
        composer_id = signal.session_id if header is not None or _looks_like_session_id(signal.session_id) else None
        found[signal.session_id] = BusyAgent(
            key=signal.session_id,
            name=name,
            composer_id=composer_id,
            workspace=workspace,
            reason=signal.marker or "transcript",
        )

    if found:
        ordered = tuple(found[key] for key in sorted(found))
        preview = ", ".join(agent.name for agent in ordered[:3])
        return ActivityDecision(
            phase=AgentPhase.BUSY,
            reason=f"local agent busy: {preview}",
            source=ordered[0].reason,
            evidence=ordered[0].composer_id or ordered[0].key,
            agents=ordered,
            warning=warning,
        )

    if waiting_markers:
        return _idle(f"waiting on user ({waiting_markers[-1]})", "transcript", warning)

    for composer in composers:
        if composer.is_background or not composer.immediate_idle:
            continue
        if composer.last_updated_at >= now - idle_seconds:
            return _idle(
                f"waiting on user ({composer.marker or 'plan-mode'})",
                "composer",
                warning,
            )

    return _idle("no recent local agent output", "none", warning)


def scan_activity(
    *,
    now: float,
    idle_seconds: float,
    cursor_running: bool | None,
    config_dir: Path,
    user_dir: Path,
    stuck_seconds: float = STUCK_GENERATING_SECONDS,
) -> ActivityDecision:
    """Read Cursor's local files and classify them. Never raises for I/O."""
    try:
        sessions, composers, warning = load_cursor_sessions(user_dir, now, stuck_seconds)
        background_ids = {sid for sid, info in sessions.items() if info.is_background}
        transcripts = collect_transcripts(
            config_dir,
            now,
            idle_seconds,
            sessions,
            background_ids,
        )
        return decide_activity(
            now=now,
            idle_seconds=idle_seconds,
            stuck_seconds=stuck_seconds,
            cursor_running=cursor_running,
            transcripts=transcripts,
            composers=composers,
            sessions=sessions,
            warning=warning,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _idle(f"detection failed ({exc})", "error", str(exc))


def transition_hold(*, phase: AgentPhase, power: str, allow_battery: bool) -> HoldTransition:
    """Map activity and power onto the desired sleep hold.

    AC power is required unless ``--allow-battery`` is set. Unknown power is
    not treated as AC, so a failed ``pmset`` read cannot keep a closed lid
    awake on battery. Idle, including a plan-mode pause, always releases.
    """
    if phase is AgentPhase.BUSY:
        if power == "ac" or (allow_battery and power in {"battery", "unknown"}):
            if power == "battery":
                return HoldTransition(True, "agent busy; holding on battery")
            if power == "unknown" and allow_battery:
                return HoldTransition(True, "agent busy; power source unknown")
            return HoldTransition(True, "agent busy")
        if power == "battery":
            return HoldTransition(False, "on battery; pass --allow-battery to hold anyway")
        return HoldTransition(False, "power source unknown; pass --allow-battery to hold anyway")
    return HoldTransition(False, "agent idle")


def display_path(path: str, home: str) -> str:
    if home and (path == home or path.startswith(home.rstrip("/") + "/")):
        suffix = path[len(home.rstrip("/")) :]
        return "~" + suffix
    return path


def _short(text: str, limit: int = 80) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def format_agent(agent: BusyAgent, *, home: str | None = None) -> str:
    """Format ``name (id ..., workspace ...)`` for one status line."""
    home_path = str(Path.home()) if home is None else home
    ident = agent.composer_id or agent.key
    label = _short(agent.name or ident)
    details: list[str] = []
    if ident and ident != label:
        details.append(f"id {ident}")
    if agent.workspace:
        details.append(f"workspace {_short(display_path(agent.workspace, home_path), 120)}")
    if not details:
        return label
    return f"{label} ({', '.join(details)})"


def plan_status_events(
    previous: MonitorSnapshot | None,
    current: MonitorSnapshot,
    *,
    hold: bool,
    verbose: bool = False,
    home: str | None = None,
) -> list[StatusStep]:
    """Build the status lines for one poll.

    Lines fire on changes only: lid, an agent becoming busy or finishing,
    and hold acquire/release. Identical polls are silent unless ``verbose``.

    When the last busy agent finishes while the hold is on, the plan order is
    ``agent finished``, then ``all local agents idle; releasing sleep hold``,
    then a ``release`` step. The daemon restores sleep and only then prints
    ``sleeping / hold released``.
    """
    steps: list[StatusStep] = []
    home_path = str(Path.home()) if home is None else home

    def log(text: str) -> None:
        steps.append(StatusStep("log", text))

    prev_agents = {} if previous is None else {agent.key: agent for agent in previous.agents}
    curr_agents = {agent.key: agent for agent in current.agents}

    if previous is None or previous.lid != current.lid:
        log(f"lid {current.lid} ({current.lid_detail})")

    if current.warning and (previous is None or previous.warning != current.warning):
        log(f"detection warning: {current.warning}")

    announced: set[str] = set()
    for key in sorted(prev_agents):
        if key not in curr_agents:
            log(f"agent finished: {format_agent(prev_agents[key], home=home_path)}")

    for key in sorted(curr_agents):
        if key not in prev_agents:
            log(f"agent busy: {format_agent(curr_agents[key], home=home_path)}")
            announced.add(key)

    # Closing the lid should show who is still working, even if that agent
    # was already announced while the lid was open.
    lid_just_closed = current.lid == "closed" and previous is not None and previous.lid != "closed"
    if lid_just_closed:
        for key in sorted(curr_agents):
            if key not in announced:
                log(f"agent busy: {format_agent(curr_agents[key], home=home_path)}")
                announced.add(key)

    want = current.want_hold
    entered_want = want and (previous is None or not previous.want_hold)
    if not hold and want and entered_want:
        for key in sorted(curr_agents):
            if key not in announced:
                log(f"agent busy: {format_agent(curr_agents[key], home=home_path)}")
                announced.add(key)
        steps.append(StatusStep("acquire", ""))
    elif hold and not want:
        if prev_agents and not curr_agents:
            log("all local agents idle; releasing sleep hold")
        elif current.block_reason:
            log(f"releasing sleep hold: {current.block_reason}")
        else:
            log("releasing sleep hold")
        steps.append(StatusStep("release", ""))
    elif prev_agents and not curr_agents:
        log("all local agents idle")
    elif curr_agents and not want and not hold:
        block = current.block_reason or "sleep hold not taken"
        changed = (
            previous is None
            or previous.want_hold
            or not previous.agents
            or previous.power != current.power
            or previous.block_reason != current.block_reason
        )
        if changed:
            log(f"not holding sleep: {block}")

    if previous is None and not curr_agents and not any(
        step.kind == "log" and ("idle" in step.text or "not running" in step.text) for step in steps
    ):
        if current.reason == "Cursor is not running":
            log("Cursor is not running")
        else:
            log("no local agent is busy")

    if verbose and not steps:
        agent_state = "busy" if curr_agents else "idle"
        hold_state = "on" if hold else "off"
        log(f"tick lid={current.lid} agents={agent_state} hold={hold_state} {current.reason}")

    return steps


def iter_agent_keys(agents: Iterable[BusyAgent]) -> list[str]:
    return sorted(agent.key for agent in agents)
