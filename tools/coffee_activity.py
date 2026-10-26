"""Heuristic detection of local Cursor agent activity.

There is no official Cursor API for "Composer is generating". This module
reads on-disk state that current Cursor releases actually write:

- ``~/.cursor/projects/<project>/agent-transcripts/**/*.jsonl``
  Local IDE and ``cursor-agent`` transcripts, including ``subagents/``.
- ``~/.cursor/chats/<workspace-hash>/<chat-id>/store.db``
  Local cursor-agent session store. ``meta.json`` beside it may hold ``cwd``.
- ``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``
  ``composerHeaders`` rows (older builds: ``ItemTable`` key
  ``composer.composerHeaders``) and ``cursorDiskKV`` keys ``composerData:<id>``.
  A set ``unfinishedRunAt`` with a fresh checkpoint means a turn is in
  progress, including plan mode. ``status == "generating"`` still counts.
  ``unifiedMode`` is ``agent``, ``chat``, ``edit``, or ``plan``.

Cloud / background agents are ignored. Their rows are tagged with ``bcId``,
a background ``unifiedMode``, or similar fields, and ``backgroundComposer``
state is not treated as local work. Those runs do not need this Mac awake.

Limitations: without ``unfinishedRunAt``, a long silent tool call can look
idle once the transcript mtime and composer ``lastUpdatedAt`` go quiet.
``--idle-seconds`` is that transcript knob. An unfinished run follows the
checkpoint instead, and a run whose checkpoint, ``lastUpdatedAt``, and
``unfinishedRunAt`` all stay still is ignored after the staleness window.
A transcript whose last record is an error ``turn_ended`` is the same kind
of unfinished turn: Cursor retries without writing, so that file stays busy
for the staleness window instead of ``--idle-seconds``. A bare ``turn_ended``
between steps does not. Wait phrases in assistant text can false-trigger on
quoted text; only the tail of the newest records is checked.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import unquote, urlparse


DEFAULT_IDLE_SECONDS = 120
DEFAULT_POLL_SECONDS = 20
# A composer left at "generating" with no writes is treated as stuck, not busy.
STUCK_GENERATING_SECONDS = 30 * 60
MAX_COMPOSER_FETCHES = 12
_TAIL_BYTES = 65_536
_WAIT_TEXT_CHARS = 800
# The topic is the opening question, so the rest of a long transcript is not read.
_TOPIC_HEAD_BYTES = 16_384
_TIMESTAMP_RE = re.compile(r"<timestamp>.*?</timestamp>", re.DOTALL)
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*(?:</user_query>|$)", re.DOTALL)

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
# Cursor writes this when the model connection fails and then retries without
# appending more transcript. It is not a clean end of the turn.
_RETRY_REASON = "retrying after a connection error"
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
    # True when the last record is an error turn_ended. That outranks a
    # pending plan: the turn failed and Cursor may still be retrying.
    retrying: bool = False


@dataclass(frozen=True)
class SessionInfo:
    composer_id: str
    name: str
    workspace: str | None
    is_background: bool
    last_updated_at: float
    unified_mode: str | None
    # Newest header stamp. Used only to choose which rows to open.
    touched_at: float = 0.0
    checkpoint_at: float = 0.0
    unfinished_run_at: float | None = None
    has_pending_plan: bool = False
    has_blocking_pending_actions: bool = False
    is_archived: bool = False
    status: str = "none"
    subtitle: str = ""


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
    checkpoint_at: float = 0.0
    unfinished_run_at: float | None = None
    waiting_on_user: bool = False


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
    # Last system sleep/wake from macOS. None means that field was not read.
    # A value on the first poll is a baseline, not a new event.
    sleep_reason: str | None = None
    wake_reason: str | None = None
    wake_type: str | None = None
    user_wake: bool | None = None
    sleep_wake_id: str | None = None
    wake_at: float | None = None
    # Closed-lid network. None means this poll did not read it (lid open, or
    # the read failed to produce a display answer). ``wifi_note`` is the
    # phrase for the lid line: Wi-Fi on, turned back on, or still down.
    external_display: bool | None = None
    wifi_note: str | None = None


@dataclass(frozen=True)
class StatusStep:
    """A terminal line, or a hold change the daemon must perform between lines.

    ``kind`` is ``log``, ``acquire``, or ``release``. The release line itself
    is printed by the daemon after the backend restores sleep, so a ``release``
    step has empty text.

    ``event`` is ``lid``, ``sleep``, or ``wake`` when the line is one of those
    monitored events. ``event_at`` is the epoch time of the event when known
    (estimated sleep start, or the real wake time). Lid lines leave it empty
    and the daemon stamps them when they are printed.
    """

    kind: str
    text: str
    event: str | None = None
    event_at: float | None = None
    # Acquire/release still runs. Quiet skips the success line. Failures print.
    quiet: bool = False


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


def _is_turn_end_record(obj: dict) -> bool:
    role = str(obj.get("role") or obj.get("kind") or "").lower()
    type_name = obj.get("type")
    type_text = str(type_name).lower() if isinstance(type_name, str) else ""
    return role in {"turn_ended", "turn-ended"} or type_text in {"turn_ended", "turn-ended"}


def _turn_end_is_error(status: object, error: object) -> bool:
    """True when Cursor ended the turn with a connection failure.

    A bare ``turn_ended`` between steps has no status and no error. ``status``
    of ``error``, or any non-empty ``error`` string, means the model call
    failed and a retry may still be in flight.
    """
    status_text = status.strip().lower() if isinstance(status, str) else ""
    error_text = error.strip() if isinstance(error, str) else ""
    return status_text == "error" or bool(error_text)


def _events_from_object(obj: dict) -> list[TranscriptEvent]:
    role = str(obj.get("role") or obj.get("kind") or "").lower()
    type_name = obj.get("type")
    type_text = str(type_name).lower() if isinstance(type_name, str) else ""
    if _is_turn_end_record(obj):
        status = obj.get("status")
        status_text = status.strip().lower() if isinstance(status, str) else ""
        error = obj.get("error")
        error_text = error.strip() if isinstance(error, str) else ""
        return [TranscriptEvent("turn_ended", error_text, status_text or None)]
    if type_name == 1 or role == "user":
        return [TranscriptEvent("user", _extract_text(obj), None)]
    if type_name == 2 or role == "assistant":
        return _assistant_events(obj)
    if role in {"tool_use", "tool"} or type_text in {"tool_use", "tool_call"}:
        name = obj.get("name") or obj.get("toolName") or obj.get("tool_name")
        payload = json.dumps(obj.get("input") or obj.get("args") or {}, default=str)[:2000]
        return [TranscriptEvent("tool", payload, str(name) if name else None)]
    return []


def _event_is_retry(event: TranscriptEvent) -> bool:
    return event.kind == "turn_ended" and _turn_end_is_error(event.tool_name, event.text)


def classify_events(events: Sequence[TranscriptEvent]) -> tuple[str, str | None]:
    """Classify the tail of one transcript.

    ``waiting`` means the agent stopped for the user (plan approval, a
    question, or an explicit wait phrase) and the hold should drop now.
    ``retrying`` means the newest record is an error ``turn_ended``. Cursor
    retries that without writing, so the hold follows the 30 minute stuck
    window instead of the idle timeout. A bare ``turn_ended`` is neither:
    agent loops write it between steps, and treating it as idle would sleep
    the Mac mid-task. Other tails stay active and follow the idle timeout
    via file mtime.
    """
    # The error has to be the newest record. A later user or assistant line
    # means the turn moved on and the idle timeout applies again.
    if events and _event_is_retry(events[-1]):
        return "retrying", _RETRY_REASON
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


def read_last_json_object(path: Path, max_bytes: int = _TAIL_BYTES) -> dict | None:
    """Parse the last complete JSON object in a transcript.

    Used for files that are already past the idle timeout. Only an error
    ``turn_ended`` in that spot can still hold sleep, so the whole tail is
    not classified.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()
            data = handle.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        return obj if isinstance(obj, dict) else None
    return None


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
    """Return the sidebar title. A subtitle such as "Edited file.py" is not one."""
    value = data.get("name")
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


def _flag_is_set(value: object) -> bool:
    return value not in (None, False, "", 0)


def _status_text(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return "none"


def _session_info_from_header(item: dict) -> SessionInfo | None:
    composer_id = item.get("composerId")
    if not isinstance(composer_id, str) or not composer_id.strip():
        return None
    mode = item.get("unifiedMode")
    mode_s = mode.strip().lower() if isinstance(mode, str) and mode.strip() else None
    last_updated = _as_epoch_seconds(item.get("lastUpdatedAt")) or 0.0
    checkpoint = _as_epoch_seconds(item.get("conversationCheckpointLastUpdatedAt")) or 0.0
    unfinished = _as_epoch_seconds(item.get("unfinishedRunAt"))
    recency = _as_epoch_seconds(item.get("recency")) or 0.0
    created = _as_epoch_seconds(item.get("createdAt")) or 0.0
    # createdAt/recency only decide which rows are worth opening. They are not
    # a busy signal: a chat can be created and then sit still.
    touched = max(last_updated, checkpoint, unfinished or 0.0, recency, created)
    subtitle = item.get("subtitle") if isinstance(item.get("subtitle"), str) else ""
    return SessionInfo(
        composer_id=composer_id,
        name=_session_name(item, composer_id),
        workspace=_workspace_from_mapping(item),
        is_background=composer_is_background(item),
        last_updated_at=last_updated,
        unified_mode=mode_s,
        touched_at=touched,
        checkpoint_at=checkpoint,
        unfinished_run_at=unfinished,
        has_pending_plan=_flag_is_set(item.get("hasPendingPlan")),
        has_blocking_pending_actions=_flag_is_set(item.get("hasBlockingPendingActions")),
        is_archived=_flag_is_set(item.get("isArchived")),
        status=_status_text(item.get("status")),
        subtitle=subtitle,
    )


def _prefer_newer_stamp(item: dict, key: str, column: object) -> None:
    """Keep the newer of a JSON field and its composerHeaders column."""
    incoming = _as_epoch_seconds(column) or 0.0
    if incoming > (_as_epoch_seconds(item.get(key)) or 0.0):
        item[key] = column


def _composer_headers_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("composerHeaders",),
    ).fetchone()
    return row is not None


def _read_composer_header_table(conn: sqlite3.Connection) -> list[dict]:
    """Read one header object per composerHeaders row.

    Current Cursor dropped the ItemTable blob. The table columns and the JSON
    ``value`` can move at different times, so a missing or older JSON stamp is
    filled from the column before any freshness check.
    """
    rows = conn.execute(
        """
        SELECT value, lastUpdatedAt, createdAt, recency, checkpointAt, isArchived
        FROM composerHeaders
        """
    ).fetchall()
    headers: list[dict] = []
    for value, last_updated, created, recency, checkpoint, is_archived in rows:
        item = _decode_json(value)
        if not isinstance(item, dict):
            continue
        _prefer_newer_stamp(item, "lastUpdatedAt", last_updated)
        _prefer_newer_stamp(item, "createdAt", created)
        _prefer_newer_stamp(item, "recency", recency)
        _prefer_newer_stamp(item, "conversationCheckpointLastUpdatedAt", checkpoint)
        if _flag_is_set(is_archived):
            item["isArchived"] = True
        headers.append(item)
    return headers


def _read_item_table_headers(conn: sqlite3.Connection) -> list[dict]:
    """Read the legacy ``composer.composerHeaders`` blob, if this Cursor still writes it."""
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


def _read_headers(conn: sqlite3.Connection) -> list[dict]:
    """Return composer headers from the schema this database actually has.

    Prefer ``composerHeaders`` when the table exists, including when it is
    empty. Falling through to the legacy ItemTable key would hide a current
    Cursor install whose old key is simply gone.
    """
    if _composer_headers_table_exists(conn):
        return _read_composer_header_table(conn)
    return _read_item_table_headers(conn)


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
    """Merge a composerData blob onto its header.

    ``data`` may be empty when the blob is missing. The header is enough for
    an unfinished run. Where both sides have a stamp, the newer one wins so a
    checkpoint that moved only in the blob is not hidden by a stale header.
    A pending plan or blocking question on either side means the user is next.
    """
    raw_id = data.get("composerId")
    composer_id = raw_id if isinstance(raw_id, str) and raw_id else info.composer_id
    blob_status = _status_text(data.get("status")) if "status" in data else ""
    status = blob_status or info.status or "none"
    mode = data.get("unifiedMode")
    mode_s = mode.strip().lower() if isinstance(mode, str) and mode.strip() else info.unified_mode
    updated = max(_as_epoch_seconds(data.get("lastUpdatedAt")) or 0.0, info.last_updated_at)
    checkpoint = max(
        _as_epoch_seconds(data.get("conversationCheckpointLastUpdatedAt")) or 0.0,
        info.checkpoint_at,
    )
    # A blob that still has the key and clears it has finished. A blob that
    # omits the key leaves the header's run flag in place.
    if "unfinishedRunAt" in data:
        unfinished = _as_epoch_seconds(data.get("unfinishedRunAt"))
    else:
        unfinished = info.unfinished_run_at
    pending = info.has_pending_plan or _flag_is_set(data.get("hasPendingPlan"))
    blocking = info.has_blocking_pending_actions or _flag_is_set(data.get("hasBlockingPendingActions"))
    waiting_on_user = pending or blocking
    name = _session_name(data, info.name)
    workspace = _workspace_from_mapping(data) or info.workspace
    background = composer_is_background(data) or info.is_background
    blob_subtitle = data.get("subtitle") if isinstance(data.get("subtitle"), str) else ""
    subtitle = blob_subtitle or info.subtitle
    immediate = False
    marker: str | None = None
    # An unfinished run is still work, including plan mode, until the user has
    # a plan to approve or a question to answer. Plan mode that is not running
    # is that pause. A generating status remains work on its own.
    run_is_active = unfinished is not None and unfinished > 0 and not waiting_on_user
    if waiting_on_user:
        immediate = True
        marker = "pending-plan" if pending else "blocking-action"
    elif not run_is_active and status not in _BUSY_STATUSES:
        if _text_is_wait(subtitle):
            immediate = True
            marker = "subtitle"
        elif mode_s == "plan":
            immediate = True
            marker = "plan-mode"
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
        checkpoint_at=checkpoint,
        unfinished_run_at=unfinished,
        waiting_on_user=waiting_on_user,
    )


def _stamp_is_fresh(stamp: float, now: float, window: float) -> bool:
    """Return whether a unix stamp is inside ``window`` and not wildly in the future."""
    if stamp <= 0:
        return False
    age = now - stamp
    return -5 <= age <= window


def _fetch_rank(info: SessionInfo) -> tuple[float, float, float]:
    """Sort key for blob reads. A fresh checkpoint outranks a fresh lastUpdatedAt."""
    return (info.checkpoint_at, info.unfinished_run_at or 0.0, info.last_updated_at)


def _newest_run_stamp(composer: ComposerSnapshot) -> float:
    return max(composer.last_updated_at, composer.checkpoint_at, composer.unfinished_run_at or 0.0)


def load_cursor_sessions(
    user_dir: Path,
    now: float,
    stuck_seconds: float,
) -> tuple[dict[str, SessionInfo], list[ComposerSnapshot], str | None]:
    """Load composer headers and recent composerData rows.

    Headers name every session (including workspace path on Cursor 3+).
    Only a few ``composerData`` blobs are opened, freshest checkpoint first,
    because those blobs are large. A header with no blob still counts.
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
        # Open rows touched inside the stuck window, freshest checkpoint first,
        # so a live run is not dropped behind idle chats that only bumped
        # lastUpdatedAt. When nothing is inside the window, still open the
        # freshest handful: composerData can be newer than the header, and
        # decide_activity drops a stamp that is actually stale.
        active = [info for info in sessions.values() if not info.is_archived]
        recent = [info for info in active if _stamp_is_fresh(info.touched_at, now, stuck_seconds)]
        if not recent:
            recent = active
        recent.sort(key=_fetch_rank, reverse=True)
        composers: list[ComposerSnapshot] = []
        for info in recent[:MAX_COMPOSER_FETCHES]:
            blob = _read_composer_blob(conn, info.composer_id)
            snapshot = _snapshot_from_composer(blob or {}, info)
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


def _retry_signal_from_quiet_transcript(path: Path) -> tuple[str, str] | None:
    """Return the retry verdict when a quiet file still ends in an error.

    Files past ``--idle-seconds`` are not fully classified. The last record
    is enough: an error ``turn_ended`` means Cursor may be retrying, and
    anything else has already gone idle.
    """
    obj = read_last_json_object(path)
    if obj is None or not _is_turn_end_record(obj):
        return None
    if not _turn_end_is_error(obj.get("status"), obj.get("error")):
        return None
    return "retrying", _RETRY_REASON


def _signal_for_file(
    path: Path,
    kind: str,
    idle_cutoff: float,
    stuck_cutoff: float,
    sessions: dict[str, SessionInfo],
    background_ids: set[str],
) -> TranscriptSignal | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    # Older than the stuck window cannot be an unfinished retry or a fresh
    # transcript. store.db has no turn_ended record, so it stops at idle.
    if mtime < stuck_cutoff:
        return None
    quiet = mtime < idle_cutoff
    if kind == "store" and quiet:
        return None
    session_id, label = _transcript_identity(path)
    if session_id in background_ids:
        return None
    workspace: str | None = None
    immediate = False
    retrying = False
    marker: str | None
    if kind == "store":
        workspace = _read_meta_cwd(path)
        marker = "store.db"
    elif quiet:
        found = _retry_signal_from_quiet_transcript(path)
        if found is None:
            return None
        _, marker = found
        retrying = True
    else:
        try:
            verdict, marker = classify_transcript_text(read_tail(path))
        except OSError:
            verdict, marker = "unknown", None
        immediate = verdict == "waiting"
        retrying = verdict == "retrying"
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
        retrying=retrying,
    )


def collect_transcripts(
    config_dir: Path,
    now: float,
    idle_seconds: float,
    sessions: dict[str, SessionInfo],
    background_ids: set[str],
    stuck_seconds: float = STUCK_GENERATING_SECONDS,
) -> list[TranscriptSignal]:
    """Stat local transcript files and classify the ones that can hold sleep.

    A file inside ``--idle-seconds`` is classified from its tail. A file that
    is quieter than that, but younger than the stuck window, is read only for
    a trailing error ``turn_ended``. Older files cannot change the hold.
    """
    idle_cutoff = now - idle_seconds
    stuck_cutoff = now - stuck_seconds
    signals: list[TranscriptSignal] = []
    projects = config_dir / "projects"
    if projects.is_dir():
        for path in projects.glob("*/agent-transcripts/**/*.jsonl"):
            if not path.is_file():
                continue
            signal = _signal_for_file(path, "jsonl", idle_cutoff, stuck_cutoff, sessions, background_ids)
            if signal is not None:
                signals.append(signal)
    chats = config_dir / "chats"
    if chats.is_dir():
        for path in chats.glob("*/*/store.db"):
            if not path.is_file():
                continue
            signal = _signal_for_file(path, "store", idle_cutoff, stuck_cutoff, sessions, background_ids)
            if signal is not None:
                signals.append(signal)
    return signals


def _looks_like_session_id(value: str) -> bool:
    return bool(_UUID_LIKE_RE.fullmatch(value))


def _name_is_placeholder(name: str, session_id: str) -> bool:
    """True when ``name`` is empty or is just the session id."""
    cleaned = " ".join(name.split())
    if not cleaned or cleaned == session_id or _looks_like_session_id(cleaned):
        return True
    prefix = "subagent "
    if cleaned.startswith(prefix):
        return _looks_like_session_id(cleaned[len(prefix) :])
    return False


def _clean_topic_text(text: str) -> str:
    """Drop Cursor's timestamp wrapper and keep the opening question."""
    without_time = _TIMESTAMP_RE.sub(" ", text)
    match = _USER_QUERY_RE.search(without_time)
    body = match.group(1) if match else without_time
    body = body.replace("<user_query>", " ").replace("</user_query>", " ")
    return _short(body)


def _first_user_topic(path: Path) -> str | None:
    """Return the first user message from the head of a transcript.

    The file can be large, and only the opening question is a useful label.
    Complete records in the head are parsed. A record longer than the head is
    recovered from the ``<user_query>`` text in that prefix.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(_TOPIC_HEAD_BYTES)
    except OSError:
        return None
    if not raw or path.suffix != ".jsonl":
        return None
    text = raw.decode("utf-8", errors="replace")
    # A partial last line is not valid JSON. Keep it only for the wrapper fallback.
    complete = text
    if len(raw) >= _TOPIC_HEAD_BYTES and "\n" in text:
        complete = text.rsplit("\n", 1)[0]
    for line in complete.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        for event in _events_from_object(obj):
            if event.kind == "user" and event.text.strip():
                topic = _clean_topic_text(event.text)
                if topic:
                    return topic
    if "<user_query>" in text:
        topic = _clean_topic_text(text)
        if topic and not topic.startswith("<"):
            return topic
    return None


def _transcript_file(config_dir: Path | None, session_id: str) -> Path | None:
    """Find the jsonl for one session, including when it is older than the idle window."""
    if config_dir is None or not _ID_RE.fullmatch(session_id):
        return None
    projects = config_dir / "projects"
    if not projects.is_dir():
        return None
    direct = sorted(projects.glob(f"*/agent-transcripts/{session_id}/{session_id}.jsonl"))
    if direct:
        return direct[0]
    nested = sorted(projects.glob(f"*/agent-transcripts/*/subagents/{session_id}.jsonl"))
    return nested[0] if nested else None


def _resolve_topic(name: str, session_id: str, path: Path | None) -> str:
    """Chat title, otherwise the first user message, otherwise the id.

    The composer ``name`` is the sidebar title. A missing title is filled from
    the transcript head so the status line is not only a uuid.
    """
    cleaned = " ".join(name.split())
    if cleaned and not _name_is_placeholder(cleaned, session_id):
        return cleaned
    if path is not None and path.suffix == ".jsonl":
        topic = _first_user_topic(path)
        if topic:
            return topic
    return cleaned or session_id


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
    config_dir: Path | None = None,
) -> ActivityDecision:
    """Decide whether any local agent is mid-task.

    Flow:
    1. If Cursor and cursor-agent are confirmed not running, idle immediately.
       ``None`` means the process list could not be read; file signals still count.
    2. Drop background/cloud composers. They must not hold this Mac awake.
    3. A local composer with ``unfinishedRunAt`` set is busy, including plan
       mode, while the newest of its checkpoint, ``lastUpdatedAt``, and that
       run stamp is inside ``stuck_seconds``. ``hasPendingPlan`` and
       ``hasBlockingPendingActions`` release immediately. A busy status with a
       fresh ``lastUpdatedAt`` still counts. A stamp that never moves again is
       ignored after ``stuck_seconds``. ``--idle-seconds`` does not clear an
       unfinished run.
    4. A transcript or ``store.db`` inside the idle window is busy unless its
       tail is an explicit user wait, or that session is already waiting on a
       plan or question. An error ``turn_ended`` stays busy out to the stuck
       window and is not cleared by that wait. Waits release immediately so a
       submitted plan does not keep the lid awake for the whole timeout. The
       same session id is not listed twice when both the composer and the
       transcript are hot.
    5. Otherwise idle.

    The busy-agent list is what the terminal prints (chat title or first user
    message, id, workspace). A composer title wins. A missing title is taken
    from the transcript head, including when that file is already outside the
    idle window.
    """
    if cursor_running is False:
        return _idle("Cursor is not running", "process", warning)

    known_paths = {signal.session_id: Path(signal.path) for signal in transcripts}

    def topic_for(name: str, session_id: str, path: Path | None = None) -> str:
        # Title first, so a named chat does not open its transcript on every poll.
        cleaned = " ".join(name.split())
        if cleaned and not _name_is_placeholder(cleaned, session_id):
            return cleaned
        candidate = path if path is not None and path.suffix == ".jsonl" else known_paths.get(session_id)
        if candidate is None or candidate.suffix != ".jsonl":
            candidate = _transcript_file(config_dir, session_id)
        return _resolve_topic(cleaned or session_id, session_id, candidate)

    found: dict[str, BusyAgent] = {}
    # A pending plan or blocking question releases even when the transcript
    # file was touched again. The user is next, so that session is not a hold.
    waiting_ids = {
        composer.composer_id
        for composer in composers
        if composer.waiting_on_user and not composer.is_background
    }
    for composer in composers:
        if composer.is_background or composer.waiting_on_user:
            continue
        # unfinishedRunAt stays set for the whole turn. Cursor often leaves
        # status at aborted/completed, and plan mode, while tools are still
        # running. The checkpoint is what keeps moving during that turn.
        if composer.unfinished_run_at:
            age = now - _newest_run_stamp(composer)
            if -5 <= age <= stuck_seconds:
                found[composer.composer_id] = BusyAgent(
                    key=composer.composer_id,
                    name=topic_for(composer.name, composer.composer_id),
                    composer_id=composer.composer_id,
                    workspace=composer.workspace,
                    reason="unfinished run",
                )
                continue
        age = now - composer.last_updated_at
        if composer.status in _BUSY_STATUSES and -5 <= age <= stuck_seconds:
            found[composer.composer_id] = BusyAgent(
                key=composer.composer_id,
                name=topic_for(composer.name, composer.composer_id),
                composer_id=composer.composer_id,
                workspace=composer.workspace,
                reason=f"status {composer.status}",
            )

    waiting_markers: list[str] = []
    for signal in transcripts:
        # A quiet error turn_ended is still inside the stuck window. The idle
        # cutoff must not drop it, and a pending plan must not either: the
        # connection failed, so the user is not actually next.
        if signal.mtime < now - idle_seconds and not signal.retrying:
            continue
        if signal.session_id in found:
            continue
        if signal.session_id in waiting_ids and not signal.retrying:
            waiting_markers.append("pending-plan")
            continue
        if signal.immediate_idle and not signal.retrying:
            waiting_markers.append(signal.marker or "waiting")
            continue
        info = sessions.get(signal.session_id)
        header = info if info is not None and not info.is_background else None
        name = header.name if header is not None else signal.label
        workspace = signal.workspace or (header.workspace if header is not None else None)
        composer_id = signal.session_id if header is not None or _looks_like_session_id(signal.session_id) else None
        found[signal.session_id] = BusyAgent(
            key=signal.session_id,
            name=topic_for(name, signal.session_id, Path(signal.path)),
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
        touched = max(composer.last_updated_at, composer.checkpoint_at)
        if _stamp_is_fresh(touched, now, idle_seconds):
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
            stuck_seconds,
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
            config_dir=config_dir,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _idle(f"detection failed ({exc})", "error", str(exc))


def transition_hold(*, phase: AgentPhase, power: str, allow_battery: bool) -> HoldTransition:
    """Map activity and power onto the desired sleep hold.

    Sleep is held on AC, on battery, and when the power source is unknown.
    ``--ac-only`` requires AC: battery and a failed ``pmset`` read do not
    hold. Idle, including a plan-mode pause, always releases.
    """
    if phase is AgentPhase.BUSY:
        if power == "ac" or (allow_battery and power in {"battery", "unknown"}):
            if power == "battery":
                return HoldTransition(True, "agent busy; holding on battery")
            if power == "unknown" and allow_battery:
                return HoldTransition(True, "agent busy; power source unknown")
            return HoldTransition(True, "agent busy")
        if power == "battery":
            return HoldTransition(False, "on battery; --ac-only requires AC power")
        return HoldTransition(False, "power source unknown; --ac-only requires AC power")
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


# A freeze shorter than this is scheduling noise, not system sleep.
_MIN_SUSPEND_SECONDS = 5.0
_USER_WAKE_HINTS = ("lid", "clamshell", "keyboard", "trackpad", "power button", "powerbutton")


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


def _format_clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _format_asleep(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    if whole < 60:
        return f"asleep ~{whole}s"
    minutes = int(round(whole / 60))
    if minutes < 60:
        return f"asleep ~{minutes}m"
    hours, leftover = divmod(minutes, 60)
    if leftover:
        return f"asleep ~{hours}h {leftover}m"
    return f"asleep ~{hours}h"


def _is_user_wake(current: MonitorSnapshot) -> bool:
    """True when the user woke the Mac, not a maintenance wake.

    ``user_wake`` is ``IOPMUserTriggeredFullWake``. When that key is missing,
    the wake reason text is the fallback. An explicit No stays a maintenance
    wake even if the reason string looks familiar.
    """
    if current.user_wake is True:
        return True
    if current.user_wake is False:
        return False
    blob = f"{current.wake_reason or ''} {current.wake_type or ''}".lower()
    return any(hint in blob for hint in _USER_WAKE_HINTS)


def _wake_cause(current: MonitorSnapshot) -> str:
    blob = f"{current.wake_reason or ''} {current.wake_type or ''}".lower()
    if "lid" in blob or "clamshell" in blob:
        return "lid"
    if "keyboard" in blob:
        return "keyboard"
    if "trackpad" in blob:
        return "trackpad"
    if "power button" in blob or "powerbutton" in blob:
        return "power button"
    return _short(current.wake_reason or current.wake_type or "unknown", 60)


def _sleep_cycle_changed(previous: MonitorSnapshot | None, current: MonitorSnapshot) -> bool:
    """True when macOS finished a sleep/wake after coffee's baseline poll.

    The first poll only records the current SleepWakeUUID and kern.waketime.
    A later poll counts as a new cycle when that id changes, or when the
    wake second changes if the id is missing. A read that fills in a value
    which was previously missing is still the baseline, not a new wake.
    """
    if previous is None:
        return False
    prev_id = previous.sleep_wake_id
    curr_id = current.sleep_wake_id
    if prev_id and curr_id and prev_id != curr_id:
        return True
    if previous.wake_at is not None and current.wake_at is not None:
        if int(previous.wake_at) != int(current.wake_at):
            return True
    return False


def _lid_line(current: MonitorSnapshot) -> str:
    """Describe the lid.

    Closed is the action that matters: coffee's decision this poll either
    keeps the Mac awake or lets it sleep. Open is just the lid. Unknown
    keeps the best-effort detail because desktops have no clamshell sensor.
    """
    if current.lid == "open":
        return "lid opened"
    if current.lid == "closed":
        if current.want_hold:
            return "lid closed; sleep hold is on, Mac stays awake"
        return "lid closed; sleep hold is off, Mac may sleep"
    return f"lid {current.lid} ({current.lid_detail})"


def _report_sleep_cycle(current: MonitorSnapshot, *, hold: bool, verbose: bool) -> bool:
    """Choose whether this sleep/wake cycle is worth a terminal line.

    A user wake is always reported. A maintenance wake is reported with
    ``verbose``, and also when the sleep hold was still on, because that
    means the hold did not keep the Mac awake.
    """
    if _is_user_wake(current) or hold:
        return True
    return verbose


def _log_sleep_wake(
    log: Callable[[str, str | None, float | None], None],
    current: MonitorSnapshot,
    *,
    suspended_seconds: float,
    hold: bool,
) -> None:
    """Print system sleep, then the wake.

    Sleep start is not a field macOS leaves on IOPMrootDomain. It is the
    wake time minus how long this process was frozen. A freeze under a few
    seconds is too small to trust, so the line then names the reason only.
    ``hold`` means the sleep hold was still on while the Mac suspended.
    """
    reason = current.sleep_reason or "sleep reason unknown"
    frozen = suspended_seconds if suspended_seconds >= _MIN_SUSPEND_SECONDS else 0.0
    sleep_at: float | None = None
    if current.wake_at is not None and frozen > 0:
        estimated = current.wake_at - frozen
        if estimated >= 0:
            sleep_at = estimated
    if sleep_at is not None:
        text = f"system sleep started about {_format_clock(sleep_at)} ({reason}, {_format_asleep(frozen)})"
    else:
        text = f"system sleep started ({reason})"
    if hold:
        text += " while the sleep hold was on"
    log(text, "sleep", sleep_at)

    cause = _wake_cause(current)
    user = _is_user_wake(current)
    verb = "woke" if user else "maintenance wake"
    if current.wake_at is None:
        log(f"{verb} ({cause})", "wake", None)
        return
    log(f"{verb} at {_format_clock(current.wake_at)} ({cause})", "wake", current.wake_at)


def _wifi_is_down(note: str | None) -> bool:
    return bool(note) and "still down" in note


def _closed_lid_summary(current: MonitorSnapshot) -> str:
    """One line for a lid close or a closed-lid decision change.

    The display clause is omitted when this poll could not read displays.
    No agents is the sleep path. Busy agents name the hold and the Wi-Fi
    result. ``Wi-Fi still down`` keeps the explicit "not finished" so a
    dead radio is not reported as the agent ending.
    """
    parts = ["lid closed"]
    if current.external_display is True:
        parts.append("clamshell (external display connected)")
    elif current.external_display is False:
        parts.append("no external display, so this is not clamshell")
    if not current.agents:
        parts.append("no local agent busy")
        parts.append("Mac may sleep")
        return "; ".join(parts)
    count = len(current.agents)
    noun = "local agent" if count == 1 else "local agents"
    parts.append(f"{count} {noun} busy")
    if current.want_hold:
        parts.append("sleep hold on")
    elif current.block_reason:
        parts.append(f"sleep hold off ({current.block_reason})")
    else:
        parts.append("sleep hold off")
    if current.wifi_note:
        parts.append(current.wifi_note)
    if _wifi_is_down(current.wifi_note):
        parts.append("agent is not finished")
    return "; ".join(parts)


def _plan_quiet_status_events(
    previous: MonitorSnapshot | None,
    current: MonitorSnapshot,
    *,
    hold: bool,
    home: str | None = None,
    suspended_seconds: float = 0.0,
) -> list[StatusStep]:
    """Lines for sleep, lid close, and a hold change while the lid stays closed.

    The lid-open stretch prints nothing about agents or the hold. The hold
    still tracks local agents, quietly, so closing the lid cannot outrun the
    next poll. A failed acquire or release is printed by the daemon itself.

    Flow:
    1. Sleep, then wake, when a cycle is worth reporting. Same rules as the
       verbose log. The first poll is a baseline.
    2. Lid open: quiet acquire or release only. After a closed lid, one
       ``lid opened`` line. A detection warning still prints.
    3. Lid just closed, the busy/idle hold flipped, or Wi-Fi crossed between
       up and still-down: one summary. Agent names follow only while someone
       is busy. Acquire is quiet because the summary already says the hold
       is on. Release is loud so the backend restore note is visible.
    """
    steps: list[StatusStep] = []
    home_path = str(Path.home()) if home is None else home

    def log(text: str, event: str | None = None, event_at: float | None = None) -> None:
        steps.append(StatusStep("log", text, event=event, event_at=event_at))

    if _sleep_cycle_changed(previous, current) and _report_sleep_cycle(current, hold=hold, verbose=False):
        _log_sleep_wake(log, current, suspended_seconds=suspended_seconds, hold=hold)

    if current.warning and (previous is None or previous.warning != current.warning):
        log(f"detection warning: {current.warning}")

    lid_closed = current.lid == "closed"
    was_closed = previous is not None and previous.lid == "closed"
    if previous is not None and was_closed and not lid_closed:
        if current.lid == "open":
            log("lid opened", event="lid")
        else:
            log(f"lid {current.lid} ({current.lid_detail})", event="lid")

    def hold_steps(quiet_acquire: bool) -> None:
        # The summary is the success line. Release stays loud so the note
        # from pmset or Amphetamine is still visible after the restore.
        if not hold and current.want_hold:
            steps.append(StatusStep("acquire", "", quiet=quiet_acquire))
        elif hold and not current.want_hold:
            # A lid-close summary already says the hold is ending. Don't add
            # a second "releasing" line in front of the backend's own note.
            announced = any(
                step.kind == "log" and (step.text.startswith("lid closed") or "releasing sleep hold" in step.text)
                for step in steps
            )
            if not announced:
                if previous is not None and previous.agents and not current.agents:
                    log("local agents idle; releasing sleep hold")
                elif current.block_reason:
                    log(f"releasing sleep hold: {current.block_reason}")
                else:
                    log("releasing sleep hold")
            steps.append(StatusStep("release", ""))

    if not lid_closed:
        hold_steps(quiet_acquire=True)
        return steps

    # Closed-lid story. Agent identity changes that do not flip the hold,
    # and a Wi-Fi radio that stays up, do not print again.
    just_closed = not was_closed
    was_busy = previous is not None and bool(previous.agents) and previous.want_hold
    now_busy = bool(current.agents) and current.want_hold
    wifi_crossed = (
        was_closed
        and _wifi_is_down(None if previous is None else previous.wifi_note) != _wifi_is_down(current.wifi_note)
        and (previous is not None and (previous.wifi_note or current.wifi_note))
    )
    decision_changed = just_closed or was_busy != now_busy or wifi_crossed or (hold and not current.want_hold)
    if decision_changed:
        if just_closed or now_busy or wifi_crossed or (current.agents and not current.want_hold):
            log(_closed_lid_summary(current), event="lid" if just_closed else None)
            if current.agents:
                for agent in current.agents:
                    log(f"  {format_agent(agent, home=home_path)}")
        elif hold and not current.want_hold:
            log("local agents idle; releasing sleep hold")
    hold_steps(quiet_acquire=True)
    return steps


def plan_status_events(
    previous: MonitorSnapshot | None,
    current: MonitorSnapshot,
    *,
    hold: bool,
    verbose: bool = False,
    home: str | None = None,
    suspended_seconds: float = 0.0,
) -> list[StatusStep]:
    """Build the status lines for one poll.

    The default is the short story: nothing while the lid is open, one
    summary when the lid closes or that closed-lid decision changes, and
    the real sleep/wake lines. ``verbose`` is the line-by-line log.
    """
    if verbose:
        return _plan_verbose_status_events(
            previous,
            current,
            hold=hold,
            verbose=True,
            home=home,
            suspended_seconds=suspended_seconds,
        )
    return _plan_quiet_status_events(
        previous,
        current,
        hold=hold,
        home=home,
        suspended_seconds=suspended_seconds,
    )


def _plan_verbose_status_events(
    previous: MonitorSnapshot | None,
    current: MonitorSnapshot,
    *,
    hold: bool,
    verbose: bool = False,
    home: str | None = None,
    suspended_seconds: float = 0.0,
) -> list[StatusStep]:
    """Build the ``--verbose`` status lines for one poll.

    Lines fire on changes only: a system sleep/wake cycle, the lid, an agent
    becoming busy or finishing, and hold acquire/release. Identical polls are
    silent unless ``verbose``.

    A sleep/wake pair is reconstructed after the Mac resumes, and it is
    printed before the lid line so the story stays in time order. The first
    poll is a baseline and does not replay a wake that happened earlier.
    Maintenance wakes are printed only with ``verbose``, unless the Mac slept
    while the sleep hold was still on.

    When the last busy agent finishes while the hold is on, the plan order is
    ``agent finished``, then ``all local agents idle; releasing sleep hold``,
    then a ``release`` step. The daemon restores sleep and only then prints
    ``sleep hold released``.
    """
    steps: list[StatusStep] = []
    home_path = str(Path.home()) if home is None else home

    def log(text: str, event: str | None = None, event_at: float | None = None) -> None:
        steps.append(StatusStep("log", text, event=event, event_at=event_at))

    prev_agents = {} if previous is None else {agent.key: agent for agent in previous.agents}
    curr_agents = {agent.key: agent for agent in current.agents}

    # The process is frozen during real system sleep, so both lines appear
    # on the poll after wake. Sleep, then wake, then a lid change.
    if _sleep_cycle_changed(previous, current) and _report_sleep_cycle(current, hold=hold, verbose=verbose):
        _log_sleep_wake(log, current, suspended_seconds=suspended_seconds, hold=hold)

    if previous is None or previous.lid != current.lid:
        log(_lid_line(current), event="lid")

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
