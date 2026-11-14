"""Tests for lf coffee activity heuristics, hold restore, and status lines."""

from __future__ import annotations

import io
import json
import os
import plistlib
import pwd
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    import typer
    from typer.testing import CliRunner
    from tools import TOOL_COMMANDS, coffee, coffee_activity, coffee_awake
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    typer = None  # type: ignore[assignment]
    CliRunner = None  # type: ignore[assignment]
    TOOL_COMMANDS = None  # type: ignore[assignment]
    coffee = None  # type: ignore[assignment]
    coffee_activity = None  # type: ignore[assignment]
    coffee_awake = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


HOME = "/Users/lf"
# Rich help styles each piece of a flag (`--idle-seconds`) in its own span on CI.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text or "")


def _agent(key: str = "abc", name: str = "Fix cache", workspace: str = "/Users/lf/src/api") -> "coffee_activity.BusyAgent":
    return coffee_activity.BusyAgent(
        key=key,
        name=name,
        composer_id=key,
        workspace=workspace,
        reason="status generating",
    )


def _snap(**overrides: object) -> "coffee_activity.MonitorSnapshot":
    payload = {
        "lid": "open",
        "lid_detail": "best-effort; AppleClamshellState=No",
        "agents": (),
        "phase": coffee_activity.AgentPhase.IDLE,
        "reason": "no recent local agent output",
        "power": "ac",
        "want_hold": False,
        "block_reason": None,
        "warning": None,
    }
    payload.update(overrides)
    return coffee_activity.MonitorSnapshot(**payload)  # type: ignore[arg-type]


def _kinds(steps: list) -> list[tuple[str, str]]:
    return [(step.kind, step.text) for step in steps]


def _user_record(text: str) -> str:
    """One transcript line shaped like Cursor's wrapped user message."""
    wrapped = (
        "<timestamp>Sunday, Oct 25, 2026, 8:00 AM (UTC+8)</timestamp>\n"
        f"<user_query>\n{text}\n</user_query>"
    )
    return json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": wrapped}]}})


@unittest.skipIf(coffee_activity is None, f"Missing dependency: {_IMPORT_ERROR}")
class TranscriptHeuristicTests(unittest.TestCase):
    def test_open_tool_call_is_active_and_plan_tools_wait(self) -> None:
        shell = json.dumps(
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Running the build."},
                        {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}},
                    ]
                },
            }
        )
        verdict, marker = coffee_activity.classify_transcript_text(shell + "\n")
        self.assertEqual(verdict, "active")
        self.assertEqual(marker, "Shell")

        plan = json.dumps(
            {
                "role": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "CreatePlan", "input": {"plan": "ship it"}}]},
            }
        )
        self.assertEqual(coffee_activity.classify_transcript_text(plan)[0], "waiting")

        question = json.dumps(
            {
                "role": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "AskQuestion", "input": {}}]},
            }
        )
        self.assertEqual(coffee_activity.classify_transcript_text(question)[0], "waiting")

    def test_wait_phrase_is_idle_but_user_text_and_turn_ended_are_not(self) -> None:
        waiting = json.dumps(
            {
                "role": "assistant",
                "message": {"content": [{"type": "text", "text": "Waiting for you to execute the plan."}]},
            }
        )
        self.assertEqual(coffee_activity.classify_transcript_text(waiting)[0], "waiting")

        quoted = json.dumps(
            {
                "role": "user",
                "message": {"content": [{"type": "text", "text": "waiting for you to execute the plan"}]},
            }
        )
        self.assertEqual(coffee_activity.classify_transcript_text(quoted)[0], "active")

        # turn_ended between steps must not count as "waiting for the user".
        ended = "\n".join(
            [
                json.dumps(
                    {
                        "role": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {"command": "make"}}]},
                    }
                ),
                json.dumps({"type": "turn_ended"}),
            ]
        )
        self.assertEqual(coffee_activity.classify_transcript_text(ended)[0], "active")

        failed = "\n".join(
            [
                json.dumps(
                    {
                        "role": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn_ended",
                        "status": "error",
                        "error": "[unavailable] HTTP/2 session not established after 20000 ms",
                    }
                ),
            ]
        )
        verdict, marker = coffee_activity.classify_transcript_text(failed)
        self.assertEqual(verdict, "retrying")
        self.assertEqual(marker, "retrying after a connection error")

    def test_cursor_process_list(self) -> None:
        self.assertTrue(coffee_activity.cursor_running_from_ps("/Applications/Cursor.app/Contents/MacOS/Cursor\n"))
        self.assertTrue(coffee_activity.cursor_running_from_ps("Cursor Helper (Plugin): extension-host\n"))
        self.assertTrue(coffee_activity.cursor_running_from_ps("cursor-agent\n"))
        self.assertFalse(coffee_activity.cursor_running_from_ps("python\nWindowServer\n"))


@unittest.skipIf(coffee_activity is None, f"Missing dependency: {_IMPORT_ERROR}")
class ScanActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.config_dir = self.root / "cursor-config"
        self.user_dir = self.root / "Cursor" / "User"
        self.now = 1_800_000_000.0
        self.addCleanup(self._temp.cleanup)

    def _write_db(self, headers: list[dict], composers: list[dict]) -> None:
        db_path = self.user_dir / "globalStorage" / "state.vscdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("composer.composerHeaders", json.dumps({"allComposers": headers})),
        )
        for composer in composers:
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (f"composerData:{composer['composerId']}", json.dumps(composer)),
            )
        conn.commit()
        conn.close()

    def _write_header_table(self, headers: list[dict], composers: list[dict] | None = None) -> None:
        """Write the current composerHeaders table and no legacy ItemTable key."""
        db_path = self.user_dir / "globalStorage" / "state.vscdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE composerHeaders (
                composerId TEXT PRIMARY KEY,
                workspaceId TEXT,
                createdAt INTEGER,
                lastUpdatedAt INTEGER,
                isArchived INTEGER,
                isSubagent INTEGER,
                recency INTEGER,
                checkpointAt INTEGER,
                value TEXT,
                subagentTypeName TEXT
            )
            """
        )
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB)")
        for item in headers:
            conn.execute(
                """
                INSERT INTO composerHeaders (
                    composerId, createdAt, lastUpdatedAt, isArchived, isSubagent,
                    recency, checkpointAt, value
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["composerId"],
                    item.get("createdAt"),
                    item.get("lastUpdatedAt"),
                    1 if item.get("isArchived") else 0,
                    1 if item.get("isSubagent") else 0,
                    item.get("recency") or item.get("lastUpdatedAt"),
                    item.get("conversationCheckpointLastUpdatedAt"),
                    json.dumps(item),
                ),
            )
        for composer in composers or []:
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (f"composerData:{composer['composerId']}", json.dumps(composer)),
            )
        conn.commit()
        conn.close()

    def _running_header(self, **overrides: object) -> dict:
        stamp = int(self.now * 1000)
        item: dict[str, object] = {
            "composerId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "name": "Toolbox repo agent issue",
            "unifiedMode": "plan",
            "status": "aborted",
            "lastUpdatedAt": stamp,
            "unfinishedRunAt": stamp,
            "conversationCheckpointLastUpdatedAt": stamp,
            "hasPendingPlan": False,
            "workspaceIdentifier": {"uri": {"fsPath": "/Users/lf/git/toolbox", "scheme": "file"}},
        }
        item.update(overrides)
        return item

    def _write_transcript(self, session_id: str, body: str, *, subagent: bool = False) -> Path:
        folder = self.config_dir / "projects" / "proj" / "agent-transcripts" / session_id
        path = folder / "subagents" / f"{session_id}.jsonl" if subagent else folder / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + "\n", encoding="utf-8")
        os.utime(path, (self.now, self.now))
        return path

    def _scan(self, *, cursor_running: bool | None = True) -> "coffee_activity.ActivityDecision":
        return coffee_activity.scan_activity(
            now=self.now,
            idle_seconds=120,
            cursor_running=cursor_running,
            config_dir=self.config_dir,
            user_dir=self.user_dir,
        )

    def test_generating_composer_is_busy_with_name_and_workspace(self) -> None:
        stamp = int(self.now * 1000)
        self._write_db(
            [
                {
                    "composerId": "abc",
                    "name": "Fix cache",
                    "lastUpdatedAt": stamp,
                    "unifiedMode": "agent",
                    "workspaceIdentifier": {"uri": {"fsPath": "/Users/lf/src/api", "scheme": "file"}},
                }
            ],
            [
                {
                    "composerId": "abc",
                    "name": "Fix cache",
                    "status": "generating",
                    "unifiedMode": "agent",
                    "lastUpdatedAt": stamp,
                }
            ],
        )
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(len(decision.agents), 1)
        agent = decision.agents[0]
        self.assertEqual(agent.name, "Fix cache")
        self.assertEqual(agent.composer_id, "abc")
        self.assertEqual(agent.workspace, "/Users/lf/src/api")

    def test_background_composer_and_its_transcript_do_not_hold(self) -> None:
        stamp = int(self.now * 1000)
        self._write_db(
            [
                {
                    "composerId": "bc-cloud",
                    "name": "Cloud job",
                    "lastUpdatedAt": stamp,
                    "unifiedMode": "background",
                    "bcId": "bc-cloud",
                }
            ],
            [
                {
                    "composerId": "bc-cloud",
                    "name": "Cloud job",
                    "status": "generating",
                    "unifiedMode": "background",
                    "bcId": "bc-cloud",
                    "lastUpdatedAt": stamp,
                }
            ],
        )
        self._write_transcript(
            "bc-cloud",
            json.dumps(
                {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {}}]}}
            ),
        )
        decision = self._scan()
        self.assertFalse(decision.busy)
        self.assertEqual(decision.agents, ())

    def test_fresh_tool_transcript_is_busy_and_plan_tail_is_idle(self) -> None:
        self._write_transcript(
            "11111111-1111-1111-1111-111111111111",
            json.dumps(
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {"command": "ls"}}]},
                }
            ),
        )
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].composer_id, "11111111-1111-1111-1111-111111111111")

        self._write_transcript(
            "22222222-2222-2222-2222-222222222222",
            json.dumps(
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "text", "text": "Waiting for user to execute the plan."}]},
                }
            ),
        )
        # The shell transcript is still fresh, so the session stays busy.
        self.assertTrue(self._scan().busy)

        for path in (self.config_dir / "projects").rglob("*.jsonl"):
            if "11111111" in path.name:
                path.unlink()
        decision = self._scan()
        self.assertFalse(decision.busy)
        self.assertIn("waiting on user", decision.reason)

    def test_titled_header_wins_over_the_first_user_message(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_header_table([self._running_header()])
        self._write_transcript(
            session_id,
            "\n".join(
                [
                    _user_record("How does coffee count two tabs?"),
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {}}]},
                        }
                    ),
                ]
            ),
        )
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].name, "Toolbox repo agent issue")
        self.assertEqual(decision.agents[0].reason, "unfinished run")

    def test_untitled_transcript_uses_the_first_user_message(self) -> None:
        session_id = "11111111-1111-1111-1111-111111111111"
        question = "How does coffee count two question tabs in this repo?"
        self._write_transcript(
            session_id,
            "\n".join(
                [
                    _user_record(question),
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
                        }
                    ),
                ]
            ),
        )
        decision = self._scan()
        self.assertTrue(decision.busy)
        agent = decision.agents[0]
        self.assertEqual(agent.name, question)
        self.assertNotEqual(agent.name, session_id)
        line = coffee_activity.format_agent(agent, home=HOME)
        self.assertIn(question, line)
        self.assertIn(f"id {session_id}", line)

    def test_long_opening_question_is_shortened(self) -> None:
        session_id = "11111111-1111-1111-1111-111111111111"
        question = "Keep the Mac awake " * 20
        self._write_transcript(
            session_id,
            "\n".join(
                [
                    _user_record(question.strip()),
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
                        }
                    ),
                ]
            ),
        )
        decision = self._scan()
        name = decision.agents[0].name
        self.assertLessEqual(len(name), 80)
        self.assertTrue(name.startswith("Keep the Mac awake"))
        self.assertTrue(name.endswith("..."))

    def test_untitled_unfinished_run_uses_the_first_user_message(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        question = "Show the chat topic while the turn is still running"
        header = self._running_header(subtitle="Edited coffee_activity.py")
        header.pop("name")
        self._write_header_table([header])
        path = self._write_transcript(
            session_id,
            "\n".join(
                [
                    _user_record(question),
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {"content": [{"type": "text", "text": "Looking at the status line."}]},
                        }
                    ),
                ]
            ),
        )
        # The transcript is already quiet. The unfinished run is what stays busy.
        os.utime(path, (self.now - 10_000, self.now - 10_000))
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].reason, "unfinished run")
        self.assertEqual(decision.agents[0].name, question)

    def test_generating_status_overrides_a_waiting_transcript_for_the_same_id(self) -> None:
        stamp = int(self.now * 1000)
        session_id = "33333333-3333-3333-3333-333333333333"
        self._write_db(
            [{"composerId": session_id, "name": "Still working", "lastUpdatedAt": stamp, "unifiedMode": "agent"}],
            [
                {
                    "composerId": session_id,
                    "name": "Still working",
                    "status": "generating",
                    "unifiedMode": "agent",
                    "lastUpdatedAt": stamp,
                }
            ],
        )
        self._write_transcript(
            session_id,
            json.dumps(
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "CreatePlan", "input": {"plan": "later"}}]},
                }
            ),
        )
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].name, "Still working")

    def test_stuck_generating_and_quit_cursor_are_idle(self) -> None:
        old = int((self.now - 4000) * 1000)
        self._write_db(
            [{"composerId": "old", "name": "Stuck", "lastUpdatedAt": old, "unifiedMode": "agent"}],
            [
                {
                    "composerId": "old",
                    "name": "Stuck",
                    "status": "generating",
                    "unifiedMode": "agent",
                    "lastUpdatedAt": old,
                }
            ],
        )
        self.assertFalse(self._scan().busy)
        self._write_transcript(
            "44444444-4444-4444-4444-444444444444",
            json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        )
        self.assertFalse(self._scan(cursor_running=False).busy)
        self.assertEqual(self._scan(cursor_running=False).reason, "Cursor is not running")

    def test_store_db_mtime_and_meta_cwd_count_as_local_work(self) -> None:
        session_id = "55555555-5555-5555-5555-555555555555"
        store = self.config_dir / "chats" / "hash" / session_id / "store.db"
        store.parent.mkdir(parents=True)
        store.write_bytes(b"")
        (store.parent / "meta.json").write_text(json.dumps({"cwd": "/Users/lf/src/cli"}), encoding="utf-8")
        os.utime(store, (self.now, self.now))
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].workspace, "/Users/lf/src/cli")

    def test_unrelated_jsonl_is_ignored(self) -> None:
        stray = self.config_dir / "projects" / "proj" / "notes" / "x.jsonl"
        stray.parent.mkdir(parents=True)
        stray.write_text("{}\n", encoding="utf-8")
        os.utime(stray, (self.now, self.now))
        self.assertFalse(self._scan().busy)

    def test_header_table_unfinished_plan_run_is_busy_without_item_table(self) -> None:
        self._write_header_table([self._running_header()])
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(len(decision.agents), 1)
        agent = decision.agents[0]
        self.assertEqual(agent.name, "Toolbox repo agent issue")
        self.assertEqual(agent.composer_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(agent.workspace, "/Users/lf/git/toolbox")
        self.assertEqual(agent.reason, "unfinished run")

    def test_pending_plan_overrides_a_fresh_tool_transcript(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_header_table(
            [
                self._running_header(
                    status="completed",
                    unfinishedRunAt=None,
                    hasPendingPlan=True,
                )
            ]
        )
        self._write_transcript(
            session_id,
            json.dumps(
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {"command": "ls"}}]},
                }
            ),
        )
        decision = self._scan()
        self.assertFalse(decision.busy)
        self.assertIn("waiting on user", decision.reason)

    def test_header_table_pending_plan_is_idle(self) -> None:
        self._write_header_table(
            [
                self._running_header(
                    name="Coffee tool event monitoring",
                    status="completed",
                    unfinishedRunAt=None,
                    hasPendingPlan=True,
                )
            ]
        )
        decision = self._scan()
        self.assertFalse(decision.busy)
        self.assertIn("waiting on user", decision.reason)

    def test_blocking_question_releases_an_unfinished_run(self) -> None:
        self._write_header_table([self._running_header(hasBlockingPendingActions=True)])
        decision = self._scan()
        self.assertFalse(decision.busy)
        self.assertIn("waiting on user", decision.reason)

    def test_stale_unfinished_run_is_idle(self) -> None:
        old = int((self.now - 4000) * 1000)
        self._write_header_table(
            [
                self._running_header(
                    name="Stuck run",
                    unifiedMode="agent",
                    lastUpdatedAt=old,
                    unfinishedRunAt=old,
                    conversationCheckpointLastUpdatedAt=old,
                )
            ]
        )
        self.assertFalse(self._scan().busy)

    def test_fresh_checkpoint_keeps_an_unfinished_run_busy_past_idle_seconds(self) -> None:
        # lastUpdatedAt is quiet for longer than --idle-seconds, but the
        # checkpoint is still moving, so the run is not stuck.
        quiet = int((self.now - 400) * 1000)
        fresh = int(self.now * 1000)
        self._write_header_table(
            [
                self._running_header(
                    lastUpdatedAt=quiet,
                    unfinishedRunAt=quiet,
                    conversationCheckpointLastUpdatedAt=fresh,
                )
            ]
        )
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].reason, "unfinished run")

    def test_newer_blob_checkpoint_keeps_a_stale_header_busy(self) -> None:
        old = int((self.now - 4000) * 1000)
        fresh = int(self.now * 1000)
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_header_table(
            [
                self._running_header(
                    name="Live via blob",
                    lastUpdatedAt=old,
                    unfinishedRunAt=old,
                    conversationCheckpointLastUpdatedAt=old,
                )
            ],
            [
                {
                    "composerId": session_id,
                    "status": "aborted",
                    "unifiedMode": "plan",
                    "lastUpdatedAt": old,
                    "unfinishedRunAt": old,
                    "conversationCheckpointLastUpdatedAt": fresh,
                }
            ],
        )
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].name, "Live via blob")
        self.assertEqual(decision.agents[0].workspace, "/Users/lf/git/toolbox")

    def test_freshest_checkpoint_is_not_dropped_behind_newer_last_updated(self) -> None:
        now_ms = int(self.now * 1000)
        headers = []
        for index in range(coffee_activity.MAX_COMPOSER_FETCHES):
            headers.append(
                {
                    "composerId": f"{index:08d}-1111-1111-1111-111111111111",
                    "name": f"Idle {index}",
                    "unifiedMode": "agent",
                    "status": "completed",
                    "lastUpdatedAt": now_ms,
                    "conversationCheckpointLastUpdatedAt": now_ms - 60_000,
                    "workspaceIdentifier": {"uri": {"fsPath": "/Users/lf/src/other", "scheme": "file"}},
                }
            )
        headers.append(
            self._running_header(
                name="Live run",
                lastUpdatedAt=now_ms - 600_000,
                unfinishedRunAt=now_ms - 600_000,
                conversationCheckpointLastUpdatedAt=now_ms,
            )
        )
        self._write_header_table(headers)
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual([agent.name for agent in decision.agents], ["Live run"])

    def test_error_turn_end_stays_busy_past_idle_and_drops_after_the_stuck_window(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_header_table(
            [
                self._running_header(
                    status="completed",
                    unfinishedRunAt=None,
                    hasPendingPlan=True,
                    lastUpdatedAt=int((self.now - 400) * 1000),
                )
            ]
        )
        path = self._write_transcript(
            session_id,
            "\n".join(
                [
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn_ended",
                            "status": "error",
                            "error": "HTTP/2 session not established",
                        }
                    ),
                ]
            ),
        )
        os.utime(path, (self.now - 300, self.now - 300))
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].reason, "retrying after a connection error")

        os.utime(path, (self.now - 31 * 60, self.now - 31 * 60))
        self.assertFalse(self._scan().busy)

        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
                        }
                    ),
                    json.dumps({"type": "turn_ended"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(path, (self.now - 300, self.now - 300))
        self.assertFalse(self._scan().busy)

    def test_unfinished_run_overrides_a_createplan_transcript(self) -> None:
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._write_header_table([self._running_header(name="Still working")])
        self._write_transcript(
            session_id,
            json.dumps(
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "CreatePlan", "input": {"plan": "later"}}]},
                }
            ),
        )
        decision = self._scan()
        self.assertTrue(decision.busy)
        self.assertEqual(decision.agents[0].name, "Still working")
        self.assertEqual(decision.agents[0].reason, "unfinished run")


@unittest.skipIf(coffee_activity is None, f"Missing dependency: {_IMPORT_ERROR}")
class HoldAndStatusTests(unittest.TestCase):
    def test_power_gate(self) -> None:
        busy = coffee_activity.AgentPhase.BUSY
        idle = coffee_activity.AgentPhase.IDLE
        self.assertTrue(coffee_activity.transition_hold(phase=busy, power="ac", allow_battery=False).hold)
        battery = coffee_activity.transition_hold(phase=busy, power="battery", allow_battery=False)
        self.assertFalse(battery.hold)
        self.assertIn("--ac-only", battery.reason)
        unknown = coffee_activity.transition_hold(phase=busy, power="unknown", allow_battery=False)
        self.assertFalse(unknown.hold)
        self.assertIn("--ac-only", unknown.reason)
        self.assertTrue(coffee_activity.transition_hold(phase=busy, power="battery", allow_battery=True).hold)
        self.assertTrue(coffee_activity.transition_hold(phase=busy, power="unknown", allow_battery=True).hold)
        self.assertFalse(coffee_activity.transition_hold(phase=idle, power="ac", allow_battery=True).hold)

    def test_session_lines_follow_lid_agent_and_hold_changes(self) -> None:
        """Lid-open work is silent. The lid close is one summary."""
        home = HOME
        idle = _snap()
        self.assertEqual(coffee_activity.plan_status_events(None, idle, hold=False, home=home), [])
        self.assertEqual(coffee_activity.plan_status_events(idle, idle, hold=False, home=home), [])

        working = _agent()
        busy = _snap(
            agents=(working,),
            phase=coffee_activity.AgentPhase.BUSY,
            reason="local agent busy: Fix cache",
            want_hold=True,
        )
        started = coffee_activity.plan_status_events(idle, busy, hold=False, home=home)
        self.assertEqual(_kinds(started), [("acquire", "")])
        self.assertTrue(started[0].quiet)
        self.assertEqual(coffee_activity.plan_status_events(busy, busy, hold=True, home=home), [])

        closed = _snap(
            lid="closed",
            lid_detail="best-effort; AppleClamshellState=Yes",
            agents=(working,),
            phase=coffee_activity.AgentPhase.BUSY,
            reason="local agent busy: Fix cache",
            want_hold=True,
            external_display=False,
            wifi_note="Wi-Fi on",
        )
        lid_close = coffee_activity.plan_status_events(busy, closed, hold=True, home=home)
        self.assertEqual(
            _kinds(lid_close),
            [
                (
                    "log",
                    "lid closed; no external display, so this is not clamshell; "
                    "1 local agent busy; sleep hold on; Wi-Fi on",
                ),
                ("log", "  Fix cache (id abc, workspace ~/src/api)"),
            ],
        )
        self.assertEqual(lid_close[0].event, "lid")

        done = _snap(
            lid="closed",
            lid_detail="best-effort; AppleClamshellState=Yes",
            external_display=False,
        )
        finished = coffee_activity.plan_status_events(closed, done, hold=True, home=home)
        self.assertEqual(
            _kinds(finished),
            [
                ("log", "local agents idle; releasing sleep hold"),
                ("release", ""),
            ],
        )
        self.assertFalse(finished[1].quiet)

    def test_one_of_two_agents_finishing_keeps_the_hold(self) -> None:
        first = _agent("aaa", "One")
        second = _agent("bbb", "Two")
        both = _snap(
            agents=(first, second),
            phase=coffee_activity.AgentPhase.BUSY,
            want_hold=True,
        )
        left = _snap(
            agents=(second,),
            phase=coffee_activity.AgentPhase.BUSY,
            want_hold=True,
        )
        steps = coffee_activity.plan_status_events(both, left, hold=True, home=HOME)
        self.assertEqual(steps, [])
        closed_both = _snap(
            lid="closed",
            agents=(first, second),
            phase=coffee_activity.AgentPhase.BUSY,
            want_hold=True,
            external_display=True,
            wifi_note="Wi-Fi on",
        )
        closed_left = _snap(
            lid="closed",
            agents=(second,),
            phase=coffee_activity.AgentPhase.BUSY,
            want_hold=True,
            external_display=True,
            wifi_note="Wi-Fi on",
        )
        self.assertEqual(
            coffee_activity.plan_status_events(closed_both, closed_left, hold=True, home=HOME),
            [],
        )

    def test_battery_announces_the_agent_without_acquiring(self) -> None:
        busy = _snap(
            agents=(_agent(),),
            phase=coffee_activity.AgentPhase.BUSY,
            power="battery",
            want_hold=False,
            block_reason="on battery; --ac-only requires AC power",
        )
        self.assertEqual(coffee_activity.plan_status_events(None, busy, hold=False, home=HOME), [])
        closed = _snap(
            lid="closed",
            agents=(_agent(),),
            phase=coffee_activity.AgentPhase.BUSY,
            power="battery",
            want_hold=False,
            block_reason="on battery; --ac-only requires AC power",
            external_display=False,
            wifi_note="Wi-Fi on",
        )
        steps = coffee_activity.plan_status_events(_snap(lid="open"), closed, hold=False, home=HOME)
        texts = [step.text for step in steps if step.kind == "log"]
        self.assertTrue(any("sleep hold off (on battery; --ac-only requires AC power)" in text for text in texts))
        self.assertTrue(any(text.startswith("  Fix cache") for text in texts))
        self.assertNotIn("acquire", [step.kind for step in steps])
        self.assertEqual(coffee_activity.plan_status_events(closed, closed, hold=False, home=HOME), [])

    def _cycle(self, **overrides: object) -> "coffee_activity.MonitorSnapshot":
        fields: dict[str, object] = {
            "sleep_reason": "Clamshell Sleep",
            "wake_reason": "EC.LidOpen",
            "wake_type": "UserActivity Assertion",
            "user_wake": True,
            "sleep_wake_id": "cycle-a",
            "wake_at": 1_700_000_000.0,
        }
        fields.update(overrides)
        return _snap(**fields)

    def test_user_wake_is_printed_before_the_lid_and_not_on_the_first_poll(self) -> None:
        asleep = self._cycle(lid="closed", sleep_wake_id="cycle-a", wake_at=1_700_000_000.0)
        first = coffee_activity.plan_status_events(None, asleep, hold=False, suspended_seconds=600)
        self.assertFalse(any(step.event in {"sleep", "wake"} for step in first))

        wake_at = 1_700_000_600.0
        start_at = wake_at - 600
        cycles = (
            coffee_activity.PowerLogEvent("sleep", start_at, "Clamshell Sleep"),
            coffee_activity.PowerLogEvent("wake", wake_at, "lid"),
        )
        awake = self._cycle(
            lid="open",
            sleep_wake_id="cycle-b",
            wake_at=wake_at,
            power_cycles=cycles,
            power_log_read=True,
        )
        steps = coffee_activity.plan_status_events(
            asleep,
            awake,
            hold=False,
            suspended_seconds=600,
            home=HOME,
        )
        start = datetime.fromtimestamp(start_at).strftime("%H:%M:%S")
        woke = datetime.fromtimestamp(wake_at).strftime("%H:%M:%S")
        self.assertEqual([step.event for step in steps], ["sleep", "lid"])
        self.assertEqual(steps[0].text, f"asleep {start}–{woke} · 10m · 1 sleep · last wake lid")
        self.assertEqual(steps[0].event_at, start_at)
        self.assertEqual(steps[1].text, "lid opened")

        held = coffee_activity.plan_status_events(asleep, awake, hold=True, suspended_seconds=600)
        self.assertIn("hold was on", held[0].text)
        self.assertEqual(held[0].event, "sleep")

    def test_same_sleep_cycle_stays_quiet_even_if_the_wait_was_long(self) -> None:
        snap = self._cycle()
        self.assertEqual(
            coffee_activity.plan_status_events(snap, snap, hold=False, suspended_seconds=600),
            [],
        )

    def test_a_filled_in_wake_time_is_not_a_new_wake(self) -> None:
        steps = coffee_activity.plan_status_events(
            _snap(),
            self._cycle(),
            hold=False,
            suspended_seconds=600,
        )
        self.assertFalse(any(step.event in {"sleep", "wake"} for step in steps))

    def test_maintenance_wake_is_verbose_unless_the_hold_was_on(self) -> None:
        before = self._cycle(
            user_wake=False,
            wake_reason="Maintenance",
            wake_type="SleepService",
            sleep_wake_id="cycle-a",
            wake_at=1_700_000_000.0,
        )
        after = self._cycle(
            user_wake=False,
            wake_reason="Maintenance",
            wake_type="SleepService",
            sleep_wake_id="cycle-b",
            wake_at=1_700_000_030.0,
        )
        quiet = coffee_activity.plan_status_events(
            before,
            after,
            hold=False,
            verbose=False,
            suspended_seconds=30,
        )
        self.assertFalse(any(step.event in {"sleep", "wake"} for step in quiet))
        noisy = coffee_activity.plan_status_events(
            before,
            after,
            hold=False,
            verbose=True,
            suspended_seconds=30,
        )
        self.assertTrue(any(step.text.startswith("unavailable for about 30s") for step in noisy))
        self.assertTrue(any("last wake maintenance" in step.text for step in noisy))
        digest = coffee_activity.SleepDigest()
        failed = coffee_activity.plan_status_events(
            before,
            after,
            hold=True,
            verbose=False,
            suspended_seconds=30,
            sleep_digest=digest,
        )
        self.assertFalse(any(step.event == "sleep" for step in failed))
        caught_up, _at = digest.catch_up()
        self.assertIn("hold was on", caught_up)
        self.assertTrue(caught_up.startswith("unavailable for about 30s"))

    def test_short_freeze_skips_the_estimated_sleep_start(self) -> None:
        before = self._cycle(sleep_wake_id="cycle-a", wake_at=1_700_000_000.0)
        after = self._cycle(sleep_wake_id="cycle-b", wake_at=1_700_000_010.0)
        steps = coffee_activity.plan_status_events(before, after, hold=False, suspended_seconds=2)
        self.assertEqual(steps[0].text, "unavailable · last wake lid")
        self.assertEqual(steps[0].event, "sleep")
        self.assertEqual(steps[0].event_at, 1_700_000_010.0)
        self.assertFalse(any(step.event == "wake" for step in steps))

    def test_lid_closed_says_whether_the_hold_keeps_the_mac_awake(self) -> None:
        held = coffee_activity.plan_status_events(
            _snap(lid="open"),
            _snap(
                lid="closed",
                agents=(_agent(),),
                phase=coffee_activity.AgentPhase.BUSY,
                want_hold=True,
                external_display=True,
                wifi_note="Wi-Fi on",
            ),
            hold=True,
        )
        self.assertEqual(
            held[0].text,
            "lid closed; clamshell (external display connected); 1 local agent busy; sleep hold on; Wi-Fi on",
        )
        self.assertEqual(held[0].event, "lid")
        restored = coffee_activity.plan_status_events(
            _snap(lid="open"),
            _snap(
                lid="closed",
                agents=(_agent(),),
                phase=coffee_activity.AgentPhase.BUSY,
                want_hold=True,
                external_display=False,
                wifi_note="Wi-Fi was off; turned it back on",
            ),
            hold=True,
        )
        self.assertIn("no external display, so this is not clamshell", restored[0].text)
        self.assertIn("Wi-Fi was off; turned it back on", restored[0].text)
        down = coffee_activity.plan_status_events(
            _snap(lid="open"),
            _snap(
                lid="closed",
                agents=(_agent(),),
                phase=coffee_activity.AgentPhase.BUSY,
                want_hold=True,
                external_display=False,
                wifi_note="Wi-Fi still down",
            ),
            hold=True,
        )
        self.assertIn("Wi-Fi still down; agent is not finished", down[0].text)
        released = coffee_activity.plan_status_events(
            _snap(lid="open"),
            _snap(lid="closed", external_display=False),
            hold=False,
        )
        self.assertEqual(
            released[0].text,
            "lid closed; no external display, so this is not clamshell; no local agent busy; Mac may sleep",
        )

    def _cycle(self, **overrides: object) -> "coffee_activity.MonitorSnapshot":
        fields: dict[str, object] = {
            "sleep_reason": "Clamshell Sleep",
            "wake_reason": "EC.LidOpen",
            "wake_type": "UserActivity Assertion",
            "user_wake": True,
            "sleep_wake_id": "cycle-a",
            "wake_at": 1_700_000_000.0,
        }
        fields.update(overrides)
        return _snap(**fields)

    def test_user_wake_is_printed_before_the_lid_and_not_on_the_first_poll(self) -> None:
        asleep = self._cycle(lid="closed", sleep_wake_id="cycle-a", wake_at=1_700_000_000.0)
        first = coffee_activity.plan_status_events(None, asleep, hold=False, suspended_seconds=600)
        self.assertFalse(any(step.event in {"sleep", "wake"} for step in first))

        wake_at = 1_700_000_600.0
        start_at = wake_at - 600
        cycles = (
            coffee_activity.PowerLogEvent("sleep", start_at, "Clamshell Sleep"),
            coffee_activity.PowerLogEvent("wake", wake_at, "lid"),
        )
        awake = self._cycle(
            lid="open",
            sleep_wake_id="cycle-b",
            wake_at=wake_at,
            power_cycles=cycles,
            power_log_read=True,
        )
        steps = coffee_activity.plan_status_events(
            asleep,
            awake,
            hold=False,
            suspended_seconds=600,
            home=HOME,
        )
        start = datetime.fromtimestamp(start_at).strftime("%H:%M:%S")
        woke = datetime.fromtimestamp(wake_at).strftime("%H:%M:%S")
        self.assertEqual([step.event for step in steps], ["sleep", "lid"])
        self.assertEqual(steps[0].text, f"asleep {start}–{woke} · 10m · 1 sleep · last wake lid")
        self.assertEqual(steps[0].event_at, start_at)
        self.assertEqual(steps[1].text, "lid opened")

        held = coffee_activity.plan_status_events(asleep, awake, hold=True, suspended_seconds=600)
        self.assertIn("hold was on", held[0].text)
        self.assertEqual(held[0].event, "sleep")

    def test_same_sleep_cycle_stays_quiet_even_if_the_wait_was_long(self) -> None:
        snap = self._cycle()
        self.assertEqual(
            coffee_activity.plan_status_events(snap, snap, hold=False, suspended_seconds=600),
            [],
        )

    def test_a_filled_in_wake_time_is_not_a_new_wake(self) -> None:
        steps = coffee_activity.plan_status_events(
            _snap(),
            self._cycle(),
            hold=False,
            suspended_seconds=600,
        )
        self.assertFalse(any(step.event in {"sleep", "wake"} for step in steps))

    def test_maintenance_wake_is_verbose_unless_the_hold_was_on(self) -> None:
        before = self._cycle(
            user_wake=False,
            wake_reason="Maintenance",
            wake_type="SleepService",
            sleep_wake_id="cycle-a",
            wake_at=1_700_000_000.0,
        )
        after = self._cycle(
            user_wake=False,
            wake_reason="Maintenance",
            wake_type="SleepService",
            sleep_wake_id="cycle-b",
            wake_at=1_700_000_030.0,
        )
        quiet = coffee_activity.plan_status_events(
            before,
            after,
            hold=False,
            verbose=False,
            suspended_seconds=30,
        )
        self.assertFalse(any(step.event in {"sleep", "wake"} for step in quiet))
        noisy = coffee_activity.plan_status_events(
            before,
            after,
            hold=False,
            verbose=True,
            suspended_seconds=30,
        )
        self.assertTrue(any(step.text.startswith("unavailable for about 30s") for step in noisy))
        self.assertTrue(any("last wake maintenance" in step.text for step in noisy))
        digest = coffee_activity.SleepDigest()
        failed = coffee_activity.plan_status_events(
            before,
            after,
            hold=True,
            verbose=False,
            suspended_seconds=30,
            sleep_digest=digest,
        )
        self.assertFalse(any(step.event == "sleep" for step in failed))
        caught_up, _at = digest.catch_up()
        self.assertIn("hold was on", caught_up)
        self.assertTrue(caught_up.startswith("unavailable for about 30s"))

    def test_short_freeze_skips_the_estimated_sleep_start(self) -> None:
        before = self._cycle(sleep_wake_id="cycle-a", wake_at=1_700_000_000.0)
        after = self._cycle(sleep_wake_id="cycle-b", wake_at=1_700_000_010.0)
        steps = coffee_activity.plan_status_events(before, after, hold=False, suspended_seconds=2)
        self.assertEqual(steps[0].text, "unavailable · last wake lid")
        self.assertEqual(steps[0].event, "sleep")
        self.assertEqual(steps[0].event_at, 1_700_000_010.0)
        self.assertFalse(any(step.event == "wake" for step in steps))

    def test_lid_closed_says_whether_the_hold_keeps_the_mac_awake(self) -> None:
        held = coffee_activity.plan_status_events(
            _snap(lid="open"),
            _snap(
                lid="closed",
                agents=(_agent(),),
                phase=coffee_activity.AgentPhase.BUSY,
                want_hold=True,
                external_display=True,
                wifi_note="Wi-Fi on",
            ),
            hold=True,
        )
        self.assertEqual(
            held[0].text,
            "lid closed; clamshell (external display connected); 1 local agent busy; sleep hold on; Wi-Fi on",
        )
        self.assertEqual(held[0].event, "lid")
        restored = coffee_activity.plan_status_events(
            _snap(lid="open"),
            _snap(
                lid="closed",
                agents=(_agent(),),
                phase=coffee_activity.AgentPhase.BUSY,
                want_hold=True,
                external_display=False,
                wifi_note="Wi-Fi was off; turned it back on",
            ),
            hold=True,
        )
        self.assertIn("no external display, so this is not clamshell", restored[0].text)
        self.assertIn("Wi-Fi was off; turned it back on", restored[0].text)
        down = coffee_activity.plan_status_events(
            _snap(lid="open"),
            _snap(
                lid="closed",
                agents=(_agent(),),
                phase=coffee_activity.AgentPhase.BUSY,
                want_hold=True,
                external_display=False,
                wifi_note="Wi-Fi still down",
            ),
            hold=True,
        )
        self.assertIn("Wi-Fi still down; agent is not finished", down[0].text)
        released = coffee_activity.plan_status_events(
            _snap(lid="open"),
            _snap(lid="closed", external_display=False),
            hold=False,
        )
        self.assertEqual(
            released[0].text,
            "lid closed; no external display, so this is not clamshell; no local agent busy; Mac may sleep",
        )

    def test_verbose_still_prints_each_agent_change(self) -> None:
        idle = _snap()
        busy = _snap(
            agents=(_agent(),),
            phase=coffee_activity.AgentPhase.BUSY,
            want_hold=True,
        )
        steps = coffee_activity.plan_status_events(idle, busy, hold=False, verbose=True, home=HOME)
        texts = [step.text for step in steps if step.kind == "log"]
        self.assertIn("agent busy: Fix cache (id abc, workspace ~/src/api)", texts)
        self.assertIn("acquire", [step.kind for step in steps])
        self.assertFalse(any(step.quiet for step in steps))

    def test_verbose_prints_quiet_polls_only(self) -> None:
        snap = _snap()
        quiet = coffee_activity.plan_status_events(snap, snap, hold=False, verbose=False, home=HOME)
        noisy = coffee_activity.plan_status_events(snap, snap, hold=False, verbose=True, home=HOME)
        self.assertEqual(quiet, [])
        self.assertEqual(len(noisy), 1)
        self.assertIn("tick", noisy[0].text)

    def test_apply_steps_releases_before_the_sleeping_line(self) -> None:
        class FakeBackend:
            name = "fake"

            def __init__(self) -> None:
                self.calls: list[str] = []

            def acquire(self) -> str:
                self.calls.append("acquire")
                return "fake-hold"

            def release(self) -> str:
                self.calls.append("release")
                return "fake-release"

        backend = FakeBackend()
        controller = coffee.HoldController(backend)
        controller.held = True
        steps = [
            coffee_activity.StatusStep("log", "agent finished: Fix cache (id abc)"),
            coffee_activity.StatusStep("log", "all local agents idle; releasing sleep hold"),
            coffee_activity.StatusStep("release", ""),
        ]
        lines: list[str] = []
        with patch("tools.coffee.emit", side_effect=lambda text, now=None: lines.append(text)):
            coffee._apply_steps(steps, controller, Path("/tmp"), on_acquire_failure=lambda: None)
        self.assertEqual(
            lines,
            [
                "agent finished: Fix cache (id abc)",
                "all local agents idle; releasing sleep hold",
                "hold off (fake-release)",
            ],
        )
        self.assertEqual(backend.calls, ["release"])
        self.assertFalse(controller.held)
        self.assertEqual(coffee.release_line(""), "hold off")

    def test_quiet_acquire_does_not_print_the_success_line(self) -> None:
        class FakeBackend:
            name = "fake"

            def acquire(self) -> str:
                return "fake-hold"

        controller = coffee.HoldController(FakeBackend())
        lines: list[str] = []
        with patch("tools.coffee.emit", side_effect=lambda text, now=None: lines.append(text)):
            coffee._apply_steps(
                [coffee_activity.StatusStep("acquire", "", quiet=True)],
                controller,
                Path("/tmp"),
                on_acquire_failure=lambda: None,
            )
        self.assertEqual(lines, [])
        self.assertTrue(controller.held)

    def test_status_shows_saved_lid_sleep_and_wake(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "state.json").write_text(
                json.dumps(
                    {
                        "backend": "pmset",
                        "hold": False,
                        "lid": "open",
                        "phase": "idle",
                        "power": "ac",
                        "events": {
                            "lid": {"text": "lid opened", "at": 1_700_000_100},
                            "sleep": {
                                "text": "system sleep started about 07:31:00 (Clamshell Sleep, asleep ~10m)",
                                "at": 1_700_000_000,
                            },
                            "wake": {"text": "woke at 07:41:21 (lid)", "at": 1_700_000_600},
                        },
                    }
                ),
                encoding="utf-8",
            )
            lines: list[str] = []
            with patch("tools.coffee.info", side_effect=lines.append):
                coffee._print_saved_state(state, running=True)
                coffee._print_saved_state(state, running=False)
        stamp = datetime.fromtimestamp(1_700_000_100).strftime("%Y-%m-%d %H:%M:%S")
        self.assertIn(f"lid event: lid opened ({stamp})", lines)
        self.assertIn(
            "sleep event: system sleep started about 07:31:00 (Clamshell Sleep, asleep ~10m)",
            lines,
        )
        self.assertIn("wake event: woke at 07:41:21 (lid)", lines)
        self.assertIn(f"last lid event: lid opened ({stamp})", lines)
        self.assertIn(
            "last sleep event: system sleep started about 07:31:00 (Clamshell Sleep, asleep ~10m)",
            lines,
        )
        self.assertIn("last wake event: woke at 07:41:21 (lid)", lines)

    def test_later_polls_keep_earlier_sleep_and_wake(self) -> None:
        events = {
            "sleep": {"text": "system sleep started (Clamshell Sleep)", "at": 10.0},
            "wake": {"text": "woke at 07:41:21 (lid)", "at": 20.0},
        }
        coffee._remember_events(
            events,
            [coffee_activity.StatusStep("log", "lid opened", event="lid")],
            now=30.0,
        )
        self.assertEqual(events["sleep"], {"text": "system sleep started (Clamshell Sleep)", "at": 10.0})
        self.assertEqual(events["wake"]["at"], 20.0)
        self.assertEqual(events["lid"], {"text": "lid opened", "at": 30.0})
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "state.json").write_text(json.dumps({"events": events}), encoding="utf-8")
            loaded = coffee._load_events(state)
        self.assertEqual(loaded["lid"]["text"], "lid opened")
        self.assertEqual(loaded["sleep"]["at"], 10.0)
        self.assertEqual(loaded["wake"]["text"], "woke at 07:41:21 (lid)")

    def test_status_shows_saved_lid_sleep_and_wake(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "state.json").write_text(
                json.dumps(
                    {
                        "backend": "pmset",
                        "hold": False,
                        "lid": "open",
                        "phase": "idle",
                        "power": "ac",
                        "events": {
                            "lid": {"text": "lid opened", "at": 1_700_000_100},
                            "sleep": {
                                "text": "system sleep started about 07:31:00 (Clamshell Sleep, asleep ~10m)",
                                "at": 1_700_000_000,
                            },
                            "wake": {"text": "woke at 07:41:21 (lid)", "at": 1_700_000_600},
                        },
                    }
                ),
                encoding="utf-8",
            )
            lines: list[str] = []
            with patch("tools.coffee.info", side_effect=lines.append):
                coffee._print_saved_state(state, running=True)
                coffee._print_saved_state(state, running=False)
        stamp = datetime.fromtimestamp(1_700_000_100).strftime("%Y-%m-%d %H:%M:%S")
        self.assertIn(f"lid event: lid opened ({stamp})", lines)
        self.assertIn(
            "sleep event: system sleep started about 07:31:00 (Clamshell Sleep, asleep ~10m)",
            lines,
        )
        self.assertIn("wake event: woke at 07:41:21 (lid)", lines)
        self.assertIn(f"last lid event: lid opened ({stamp})", lines)
        self.assertIn(
            "last sleep event: system sleep started about 07:31:00 (Clamshell Sleep, asleep ~10m)",
            lines,
        )
        self.assertIn("last wake event: woke at 07:41:21 (lid)", lines)

    def test_later_polls_keep_earlier_sleep_and_wake(self) -> None:
        events = {
            "sleep": {"text": "system sleep started (Clamshell Sleep)", "at": 10.0},
            "wake": {"text": "woke at 07:41:21 (lid)", "at": 20.0},
        }
        coffee._remember_events(
            events,
            [coffee_activity.StatusStep("log", "lid opened", event="lid")],
            now=30.0,
        )
        self.assertEqual(events["sleep"], {"text": "system sleep started (Clamshell Sleep)", "at": 10.0})
        self.assertEqual(events["wake"]["at"], 20.0)
        self.assertEqual(events["lid"], {"text": "lid opened", "at": 30.0})
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "state.json").write_text(json.dumps({"events": events}), encoding="utf-8")
            loaded = coffee._load_events(state)
        self.assertEqual(loaded["lid"]["text"], "lid opened")
        self.assertEqual(loaded["sleep"]["at"], 10.0)
        self.assertEqual(loaded["wake"]["text"], "woke at 07:41:21 (lid)")

    def test_stale_wake_time_is_not_printed(self) -> None:
        before = self._cycle(sleep_wake_id="cycle-a", wake_at=1_700_000_600.0)
        after = self._cycle(sleep_wake_id="cycle-b", wake_at=1_700_000_000.0, power_log_read=True)
        steps = coffee_activity.plan_status_events(before, after, hold=False, suspended_seconds=2)
        self.assertFalse(any(step.event in {"sleep", "wake"} for step in steps))

    def test_power_log_counts_every_cycle_in_one_freeze(self) -> None:
        start = 1_700_000_000.0
        cycles = (
            coffee_activity.PowerLogEvent("sleep", start, "Idle Sleep"),
            coffee_activity.PowerLogEvent("darkwake", start + 70, "maintenance"),
            coffee_activity.PowerLogEvent("sleep", start + 100, "Maintenance Sleep"),
            coffee_activity.PowerLogEvent("darkwake", start + 200, "maintenance"),
            coffee_activity.PowerLogEvent("sleep", start + 230, "Maintenance Sleep"),
            coffee_activity.PowerLogEvent("wake", start + 550, "power button"),
        )
        before = self._cycle(sleep_wake_id="cycle-a", wake_at=start - 10)
        after = self._cycle(
            lid="open",
            sleep_wake_id="cycle-b",
            wake_at=start + 550,
            power_cycles=cycles,
            power_log_read=True,
        )
        steps = coffee_activity.plan_status_events(before, after, hold=False, suspended_seconds=550)
        self.assertEqual(steps[0].event, "sleep")
        self.assertIn("3 sleeps, 2 dark wakes", steps[0].text)
        self.assertIn("last wake power button", steps[0].text)
        self.assertNotIn("system sleep started", steps[0].text)
        self.assertFalse(any(step.event == "wake" for step in steps))

        listed = coffee_activity.plan_status_events(
            before,
            after,
            hold=True,
            verbose=True,
            suspended_seconds=550,
        )
        texts = [step.text for step in listed]
        self.assertEqual(sum(text.startswith("sleep ") for text in texts), 3)
        self.assertEqual(sum(text.startswith("dark wake ") for text in texts), 2)
        self.assertTrue(any(text.startswith("asleep ") and "hold was on" in text for text in texts))

    def test_dark_wakes_alone_stay_quiet_unless_the_hold_failed(self) -> None:
        start = 1_700_000_000.0
        cycles = (
            coffee_activity.PowerLogEvent("sleep", start, "Maintenance Sleep"),
            coffee_activity.PowerLogEvent("darkwake", start + 20, "maintenance"),
        )
        before = self._cycle(user_wake=False, sleep_wake_id="cycle-a", wake_at=start - 30)
        after = self._cycle(
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-b",
            wake_at=start + 20,
            power_cycles=cycles,
            power_log_read=True,
        )
        quiet = coffee_activity.plan_status_events(before, after, hold=False, verbose=False)
        self.assertFalse(any(step.event == "sleep" for step in quiet))
        digest = coffee_activity.SleepDigest()
        failed = coffee_activity.plan_status_events(
            before, after, hold=True, verbose=False, sleep_digest=digest
        )
        self.assertFalse(any(step.event == "sleep" for step in failed))
        caught_up, _at = digest.catch_up()
        self.assertIn("hold was on", caught_up)
        self.assertIn("1 dark wake", caught_up)

    def test_dark_wake_rows_that_look_like_a_user_wake_stay_in_the_digest(self) -> None:
        start = 1_700_000_000.0
        reason = "DarkWake from Deep Idle [CDNP] : due to smc.sysState.Wake"
        cycles = (
            coffee_activity.PowerLogEvent("sleep", start, "Idle Sleep"),
            coffee_activity.PowerLogEvent("darkwake", start + 20, reason),
            coffee_activity.PowerLogEvent("wake", start + 20, reason),
        )
        before = self._cycle(lid="closed", user_wake=False, sleep_wake_id="cycle-a", wake_at=start - 30)
        after = self._cycle(
            lid="closed",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-b",
            wake_at=start + 20,
            power_cycles=cycles,
            power_log_read=True,
        )
        digest = coffee_activity.SleepDigest()
        steps = coffee_activity.plan_status_events(before, after, hold=False, sleep_digest=digest)
        self.assertFalse(any(step.event == "sleep" for step in steps))
        self.assertTrue(digest.pending)
        maintenance = (
            coffee_activity.PowerLogEvent("sleep", start + 40, "Maintenance Sleep"),
            coffee_activity.PowerLogEvent("wake", start + 50, "maintenance"),
        )
        later = self._cycle(
            lid="closed",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-c",
            wake_at=start + 50,
            power_cycles=maintenance,
            power_log_read=True,
        )
        stayed = coffee_activity.plan_status_events(after, later, hold=False, sleep_digest=digest)
        self.assertFalse(any(step.event == "sleep" for step in stayed))
        self.assertTrue(digest.pending)

    def test_dark_wakes_catch_up_when_the_lid_opens(self) -> None:
        start = 1_700_000_000.0
        digest = coffee_activity.SleepDigest()
        base = self._cycle(
            lid="closed",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-a",
            wake_at=start - 10,
        )
        first_gap = (
            coffee_activity.PowerLogEvent("sleep", start, "Idle Sleep"),
            coffee_activity.PowerLogEvent("darkwake", start + 60, "maintenance"),
            coffee_activity.PowerLogEvent("wake", start + 60, "maintenance"),
        )
        first = self._cycle(
            lid="closed",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-b",
            wake_at=start + 60,
            power_cycles=first_gap,
            power_log_read=True,
        )
        second_gap = (
            coffee_activity.PowerLogEvent("sleep", start + 120, "Maintenance Sleep"),
            coffee_activity.PowerLogEvent("darkwake", start + 200, "maintenance"),
        )
        second = self._cycle(
            lid="closed",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-c",
            wake_at=start + 200,
            power_cycles=second_gap,
            power_log_read=True,
        )
        self.assertFalse(
            any(
                step.event == "sleep"
                for step in coffee_activity.plan_status_events(base, first, hold=False, sleep_digest=digest)
            )
        )
        self.assertFalse(
            any(
                step.event == "sleep"
                for step in coffee_activity.plan_status_events(first, second, hold=False, sleep_digest=digest)
            )
        )
        opening = (
            coffee_activity.PowerLogEvent("sleep", start + 300, "Clamshell Sleep"),
            coffee_activity.PowerLogEvent("wake", start + 400, "lid"),
        )
        opened = self._cycle(
            lid="open",
            user_wake=True,
            wake_reason="EC.LidOpen",
            sleep_wake_id="cycle-d",
            wake_at=start + 400,
            power_cycles=opening,
            power_log_read=True,
        )
        steps = coffee_activity.plan_status_events(second, opened, hold=False, sleep_digest=digest)
        self.assertEqual([step.event for step in steps], ["sleep", "lid"])
        self.assertIn("3 sleeps, 2 dark wakes", steps[0].text)
        self.assertIn("last wake lid", steps[0].text)
        self.assertEqual(steps[0].event_at, start)
        self.assertEqual(steps[1].text, "lid opened")
        self.assertFalse(digest.pending)

    def test_opening_the_lid_flushes_without_a_new_power_row(self) -> None:
        start = 1_700_000_000.0
        digest = coffee_activity.SleepDigest()
        closed = self._cycle(
            lid="closed",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-a",
            wake_at=start,
        )
        dark = self._cycle(
            lid="closed",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-b",
            wake_at=start + 20,
            power_cycles=(
                coffee_activity.PowerLogEvent("sleep", start, "Idle Sleep"),
                coffee_activity.PowerLogEvent("darkwake", start + 20, "maintenance"),
            ),
            power_log_read=True,
        )
        self.assertFalse(
            any(
                step.event == "sleep"
                for step in coffee_activity.plan_status_events(closed, dark, hold=False, sleep_digest=digest)
            )
        )
        opened = self._cycle(
            lid="open",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-b",
            wake_at=start + 20,
        )
        steps = coffee_activity.plan_status_events(dark, opened, hold=False, sleep_digest=digest)
        self.assertEqual([step.event for step in steps], ["sleep", "lid"])
        self.assertIn("1 dark wake", steps[0].text)
        self.assertEqual(steps[1].text, "lid opened")
        self.assertFalse(digest.pending)

    def test_keyboard_wake_flushes_even_when_the_lid_stayed_open(self) -> None:
        start = 1_700_000_000.0
        digest = coffee_activity.SleepDigest()
        before = self._cycle(lid="open", user_wake=False, sleep_wake_id="cycle-a", wake_at=start - 30)
        dark = (
            coffee_activity.PowerLogEvent("sleep", start, "Idle Sleep"),
            coffee_activity.PowerLogEvent("darkwake", start + 20, "maintenance"),
        )
        asleep = self._cycle(
            lid="open",
            user_wake=False,
            wake_reason="Maintenance",
            sleep_wake_id="cycle-b",
            wake_at=start + 20,
            power_cycles=dark,
            power_log_read=True,
        )
        quiet = coffee_activity.plan_status_events(before, asleep, hold=False, sleep_digest=digest)
        self.assertFalse(any(step.event == "sleep" for step in quiet))
        keyed = self._cycle(
            lid="open",
            user_wake=True,
            wake_reason="HID Activity keyboard",
            sleep_wake_id="cycle-c",
            wake_at=start + 100,
            power_cycles=(coffee_activity.PowerLogEvent("wake", start + 100, "keyboard"),),
            power_log_read=True,
        )
        steps = coffee_activity.plan_status_events(asleep, keyed, hold=False, sleep_digest=digest)
        self.assertEqual([step.event for step in steps], ["sleep"])
        self.assertIn("last wake keyboard", steps[0].text)
        self.assertIn("1 dark wake", steps[0].text)
        self.assertEqual(steps[0].event_at, start)
        self.assertFalse(digest.pending)

    def test_lid_open_release_does_not_log(self) -> None:
        busy = _snap(
            agents=(_agent(),),
            phase=coffee_activity.AgentPhase.BUSY,
            want_hold=True,
        )
        steps = coffee_activity.plan_status_events(busy, _snap(), hold=True, home=HOME)
        self.assertEqual([step.kind for step in steps], ["release"])
        self.assertTrue(steps[0].quiet)
        self.assertEqual(steps[0].text, "")

    def test_live_status_prefers_the_sleep_total(self) -> None:
        snap = _snap()
        self.assertEqual(
            coffee_activity.format_live_status(snap, hold=False),
            "lid open · idle · hold off",
        )
        busy = _snap(
            agents=(_agent(),),
            phase=coffee_activity.AgentPhase.BUSY,
            want_hold=True,
        )
        self.assertEqual(
            coffee_activity.format_live_status(busy, hold=True),
            "lid open · 1 local agent · hold on",
        )
        digest = coffee_activity.SleepDigest()
        start = 1_700_000_000.0
        digest.cycles.extend(
            [
                coffee_activity.PowerLogEvent("sleep", start, "Idle Sleep"),
                coffee_activity.PowerLogEvent("darkwake", start + 3600, "maintenance"),
            ]
        )
        self.assertEqual(
            coffee_activity.format_live_status(snap, hold=False, digest=digest),
            "asleep · 1h · 1 dark wake · maintenance",
        )

    def test_closed_lid_does_not_claim_the_hold_before_it_is_set(self) -> None:
        closed = _snap(
            lid="closed",
            agents=(_agent(),),
            phase=coffee_activity.AgentPhase.BUSY,
            want_hold=True,
            external_display=False,
            wifi_note="Wi-Fi on",
        )
        steps = coffee_activity.plan_status_events(_snap(lid="open"), closed, hold=False, home=HOME)
        texts = [step.text for step in steps if step.kind == "log"]
        self.assertTrue(any("1 local agent busy" in text for text in texts))
        self.assertFalse(any("sleep hold on" in text or text == "hold on" for text in texts))
        acquire = [step for step in steps if step.kind == "acquire"]
        self.assertEqual(len(acquire), 1)
        self.assertFalse(acquire[0].quiet)

    def test_pmset_log_parser_keeps_only_the_gap(self) -> None:
        sample = "\n".join(
            [
                "2026-11-07 23:21:00 +0800 Wake                 Wake from Deep Idle : due to lid",
                "2026-11-07 23:23:54 +0800 Sleep                Entering Sleep state due to 'Idle Sleep':",
                "2026-11-07 23:25:10 +0800 DarkWake             DarkWake from Deep Idle : due to RTC/Maintenance",
                "2026-11-07 23:25:40 +0800 Sleep                Entering Sleep state due to 'Maintenance Sleep':",
                "2026-11-07 23:28:00 +0800 DarkWake             DarkWake from Deep Idle : due to RTC",
                "2026-11-07 23:28:20 +0800 Sleep                Entering Sleep state due to 'Maintenance Sleep':",
                "2026-11-07 23:33:04 +0800 Wake                 Wake from Deep Idle : due to smc pwrbtn SMC.OutboxNotEmpty",
            ]
        )
        start = datetime.strptime("2026-11-07 23:22:00 +0800", "%Y-%m-%d %H:%M:%S %z").timestamp()
        end = datetime.strptime("2026-11-07 23:33:04 +0800", "%Y-%m-%d %H:%M:%S %z").timestamp()
        cycles = coffee_activity.parse_pmset_log(sample, start=start, end=end)
        self.assertEqual([item.kind for item in cycles], ["sleep", "darkwake", "sleep", "darkwake", "sleep", "wake"])
        self.assertEqual(cycles[0].reason, "Idle Sleep")
        self.assertEqual(cycles[-1].reason, "power button")
        text = coffee_activity.format_power_summary(cycles, hold=False)
        self.assertIn("3 sleeps, 2 dark wakes", text)
        self.assertIn("last wake power button", text)
        self.assertNotIn("23:21:00", text)

    def test_startup_lines_stay_short_until_the_password(self) -> None:
        missing = coffee_awake.assess_amphetamine(
            force_pmset=False,
            installed=False,
            usable=False,
            closed_display_sleep_allowed=None,
        )
        config = coffee.CoffeeConfig(
            idle_seconds=120,
            poll_seconds=20,
            force_pmset=False,
            allow_battery=True,
            dry_run=False,
            background=False,
            verbose=False,
        )
        with patch.object(coffee.os, "geteuid", return_value=501):
            lines = coffee.startup_lines(config, "pmset", missing)
        self.assertEqual(
            lines,
            (
                "coffee · pmset · idle 120s · poll 20s",
                "Amphetamine is not installed. Password once for this run.",
            ),
        )
        ready = coffee_awake.assess_amphetamine(
            force_pmset=False,
            installed=True,
            usable=True,
            closed_display_sleep_allowed=False,
        )
        self.assertEqual(
            coffee.startup_lines(config, "amphetamine", ready),
            ("coffee · amphetamine · idle 120s · poll 20s",),
        )
        dry = coffee.CoffeeConfig(
            idle_seconds=120,
            poll_seconds=20,
            force_pmset=True,
            allow_battery=True,
            dry_run=True,
            background=False,
            verbose=False,
        )
        self.assertIn("dry-run", coffee.startup_lines(dry, "pmset", missing)[1])

    def test_paint_colors_keywords_only_when_asked(self) -> None:
        plain = "lid closed; sleep hold on; Wi-Fi still down; hold was on"
        self.assertEqual(coffee.paint(plain, enabled=False), plain)
        colored = coffee.paint(plain, enabled=True)
        self.assertIn("lid closed", _plain(colored))
        self.assertNotEqual(colored, plain)
        self.assertIn("\x1b[", colored)

    def test_session_starts_only_when_the_hold_can_be_taken(self) -> None:
        self.assertEqual(coffee.session_start_line(hold_available=True), "coffee session started")
        self.assertIsNone(coffee.session_start_line(hold_available=False))

    def test_live_line_rewrites_only_when_enabled(self) -> None:
        stream = io.StringIO()
        quiet = coffee.LiveLine(stream, enabled=False)
        quiet.draw("lid open · idle · hold off")
        quiet.break_for_log()
        self.assertEqual(stream.getvalue(), "")

        shown = io.StringIO()
        line = coffee.LiveLine(shown, enabled=True)
        line.draw("lid open · idle · hold off")
        self.assertIn("\r", shown.getvalue())
        self.assertIn("\x1b[K", shown.getvalue())
        self.assertIn("lid open · idle · hold off", shown.getvalue())
        after_first = shown.getvalue()
        line.draw("lid open · idle · hold off")
        self.assertEqual(shown.getvalue(), after_first)
        line.draw("lid open · 1 local agent · hold on")
        self.assertEqual(shown.getvalue().count("\r"), 2)
        self.assertIn("1 local agent", shown.getvalue())

    def test_emit_breaks_the_live_line_before_the_log_row(self) -> None:
        stream = io.StringIO()
        line = coffee.LiveLine(stream, enabled=True)
        line.draw("lid open · idle · hold off")
        seen: list[str] = []

        def _info(message: str) -> None:
            self.assertIn("\n", stream.getvalue())
            seen.append(message)

        coffee._LIVE = line
        try:
            with patch("tools.coffee.info", side_effect=_info):
                coffee.emit("coffee session started", now=datetime(2026, 11, 13, 13, 42, 50))
        finally:
            coffee._LIVE = None
        self.assertEqual(seen, ["2026-11-13 13:42:50  coffee session started"])
        self.assertFalse(line._open)
        line.close()
        self.assertTrue(stream.getvalue().endswith("\n"))

    def test_amphetamine_session_failure_announces_the_pmset_switch(self) -> None:
        class AmpFail:
            name = "amphetamine"

            def acquire(self) -> str:
                raise coffee_awake.CommandError(["osascript"], 1, "app not running")

        class PmsetOK:
            name = "pmset"

            def acquire(self) -> str:
                return "sudo pmset -a disablesleep 1"

        controller = coffee.HoldController(
            AmpFail(),
            backend_reason="Amphetamine was selected",
            amphetamine_state="ready",
        )
        lines: list[str] = []
        with (
            patch("tools.coffee.emit", side_effect=lambda text, now=None: lines.append(text)),
            patch("tools.coffee.awake.PmsetBackend", return_value=PmsetOK()),
        ):
            coffee._apply_steps(
                [coffee_activity.StatusStep("acquire", "")],
                controller,
                Path("/tmp"),
                on_acquire_failure=lambda: None,
            )
        text = "\n".join(lines)
        self.assertIn("not usable", text)
        self.assertIn("Allow system sleep when display is closed", text)
        self.assertIn("Using pmset instead", text)
        self.assertIn("hold on (sudo pmset -a disablesleep 1)", text)
        self.assertLess(
            text.index("Using pmset instead"),
            text.index("hold on"),
        )
        self.assertEqual(controller.amphetamine_state, "not-usable")
        self.assertIn("using pmset instead", controller.backend_reason)
        self.assertTrue(controller.held)


@unittest.skipIf(coffee_awake is None, f"Missing dependency: {_IMPORT_ERROR}")
class AwakeBackendTests(unittest.TestCase):
    def test_clamshell_and_power_parsing(self) -> None:
        closed, token = coffee_awake.parse_clamshell_state(
            '+-o IOPMrootDomain\n  "AppleClamshellState" = Yes\n  "AppleClamshellCausesSleep" = Yes\n'
        )
        self.assertEqual((closed, token), ("closed", "Yes"))
        opened, _token = coffee_awake.parse_clamshell_state('"AppleClamshellState" = No')
        self.assertEqual(opened, "open")
        self.assertEqual(coffee_awake.parse_clamshell_state('"AppleClamshellState" = 1')[0], "closed")
        self.assertEqual(coffee_awake.parse_clamshell_state("no clamshell here")[0], "unknown")

        ac = "Now drawing from 'AC Power'\n -InternalBattery-0 100%; charged\n"
        battery = "Now drawing from 'Battery Power'\n -InternalBattery-0 80%; discharging\n"
        self.assertEqual(coffee_awake.parse_power_source(ac), "ac")
        self.assertEqual(coffee_awake.parse_power_source(battery), "battery")
        self.assertEqual(coffee_awake.parse_disablesleep(" disablesleep         1\n"), 1)
        self.assertEqual(coffee_awake.parse_disablesleep("SleepDisabled\t0\n"), 0)
        self.assertIsNone(coffee_awake.parse_disablesleep("hibernatemode 3\n"))

        state = coffee_awake.read_lid_state(
            lambda argv, interactive=False, timeout=30: coffee_awake.CommandResult(
                0, '"AppleClamshellState" = Yes\n', ""
            )
        )
        self.assertEqual(state.position, "closed")
        self.assertIn("best-effort", state.detail)

    def _network_run(self, *, display: str, power: list[str], route: str):
        """Fake ``run`` for the closed-lid network check.

        ``power`` is consumed in order so a radio that was off can read back on
        after ``setairportpower``.
        """
        powers = list(power)
        calls: list[list[str]] = []

        def run(argv: list[str], interactive: bool = False, timeout: float = 30) -> "coffee_awake.CommandResult":
            del interactive, timeout
            calls.append(list(argv))
            command = " ".join(argv)
            if argv[0] == "system_profiler":
                return coffee_awake.CommandResult(0, display, "")
            if "listallhardwareports" in command:
                return coffee_awake.CommandResult(0, "Hardware Port: Wi-Fi\nDevice: en0\n", "")
            if "getairportpower" in command:
                current = powers.pop(0) if powers else "On"
                return coffee_awake.CommandResult(0, f"Wi-Fi Power (en0): {current}\n", "")
            if "setairportpower" in command:
                return coffee_awake.CommandResult(0, "", "")
            if argv[:1] == ["route"]:
                return coffee_awake.CommandResult(0, route, "")
            return coffee_awake.CommandResult(1, "", "unexpected")

        return run, calls

    def test_no_display_turns_wifi_back_on_and_names_clamshell(self) -> None:
        self.assertFalse(coffee_awake.parse_external_display("Connection Type: Internal\n"))
        self.assertTrue(
            coffee_awake.parse_external_display("Connection Type: Internal\nConnection Type: Thunderbolt\n")
        )
        self.assertIsNone(coffee_awake.parse_external_display(""))
        self.assertEqual(
            coffee_awake.parse_wifi_device("Hardware Port: Wi-Fi\nDevice: en0\n"),
            "en0",
        )
        self.assertEqual(coffee_awake.parse_airport_power("Wi-Fi Power (en0): Off\n"), "off")
        self.assertTrue(coffee_awake.parse_has_default_route("gateway: 10.0.0.1\n"))

        run, calls = self._network_run(
            display="Connection Type: Internal\n",
            power=["Off", "On"],
            route="gateway: 10.0.0.1\n",
        )
        report = coffee_awake.assess_closed_lid_network(run, restore_wifi=True)
        self.assertFalse(report.external_display)
        self.assertEqual(report.wifi_note, "Wi-Fi was off; turned it back on")
        self.assertTrue(any("setairportpower" in " ".join(command) for command in calls))

        run, calls = self._network_run(
            display="Connection Type: Internal\nConnection Type: Thunderbolt\n",
            power=["Off"],
            route="gateway: 10.0.0.1\n",
        )
        report = coffee_awake.assess_closed_lid_network(run, restore_wifi=True)
        self.assertTrue(report.external_display)
        self.assertEqual(report.wifi_note, "Wi-Fi still down")
        self.assertFalse(any("setairportpower" in " ".join(command) for command in calls))

        run, _calls = self._network_run(
            display="Connection Type: Internal\n",
            power=["On"],
            route="",
        )
        report = coffee_awake.assess_closed_lid_network(run, restore_wifi=True)
        self.assertEqual(report.wifi_note, "Wi-Fi still down")

    def test_sleep_and_wake_are_read_from_ioreg_and_kern_waketime(self) -> None:
        sample = "\n".join(
            [
                '"AppleClamshellState" = No',
                '"Last Sleep Reason" = "Clamshell Sleep"',
                '"Wake Reason" = "smc.sysState.Wake(0x70070000) lid SMC.OutboxNotEmpty"',
                '"Wake Type" = "UserActivity Assertion"',
                '"IOPMUserTriggeredFullWake" = Yes',
                '"SleepWakeUUID" = "065A60A2-2064-4D83-BC07-77549E987299"',
            ]
        )
        parsed = coffee_awake.parse_power_events(sample)
        self.assertEqual(parsed.sleep_reason, "Clamshell Sleep")
        self.assertIn("lid", parsed.wake_reason or "")
        self.assertEqual(parsed.wake_type, "UserActivity Assertion")
        self.assertIs(parsed.user_wake, True)
        self.assertEqual(parsed.sleep_wake_id, "065A60A2-2064-4D83-BC07-77549E987299")
        self.assertIsNone(parsed.wake_at)
        self.assertIs(coffee_awake.parse_power_events('"IOPMUserTriggeredFullWake" = No\n').user_wake, False)
        self.assertIsNone(coffee_awake.parse_power_events("nothing here").sleep_wake_id)
        self.assertIsNone(coffee_awake.parse_waketime("no time"))
        self.assertEqual(coffee_awake.parse_waketime("{ sec = 100, usec = 500000 }\n"), 100.5)
        self.assertEqual(coffee_awake.parse_waketime("{ sec = 100 }\n"), 100.0)

        calls: list[list[str]] = []

        def run(argv, interactive=False, timeout=30):
            del interactive, timeout
            calls.append(list(argv))
            if argv[0] == "sysctl":
                return coffee_awake.CommandResult(0, "{ sec = 1792885281, usec = 416992 }\n", "")
            return coffee_awake.CommandResult(0, sample + "\n", "")

        platform = coffee_awake.read_platform_state(run)
        self.assertEqual(platform.lid.position, "open")
        self.assertEqual(platform.power.sleep_reason, "Clamshell Sleep")
        self.assertIs(platform.power.user_wake, True)
        self.assertEqual(platform.power.sleep_wake_id, "065A60A2-2064-4D83-BC07-77549E987299")
        self.assertAlmostEqual(platform.power.wake_at or 0, 1792885281.416992)
        ioreg_calls = [argv for argv in calls if argv and argv[0] == "ioreg"]
        self.assertEqual(len(ioreg_calls), 1)
        self.assertEqual(calls[-1][:3], ["sysctl", "-n", "kern.waketime"])

    def _ready(self, **kwargs: object) -> "coffee_awake.AmphetamineReadiness":
        fields = {
            "force_pmset": False,
            "installed": True,
            "usable": True,
            "closed_display_sleep_allowed": False,
        }
        fields.update(kwargs)
        return coffee_awake.assess_amphetamine(**fields)  # type: ignore[arg-type]

    def test_backend_choice_and_amphetamine_script(self) -> None:
        ready = self._ready()
        self.assertEqual(ready.state, "ready")
        self.assertEqual(
            coffee_awake.choose_backend(ready),
            (
                "amphetamine",
                "Amphetamine is installed and Session Defaults disallow sleep when the display is closed",
            ),
        )
        skipped = self._ready(force_pmset=True, installed=False, usable=False, closed_display_sleep_allowed=None)
        self.assertEqual(skipped.state, "skipped")
        self.assertEqual(coffee_awake.choose_backend(skipped)[0], "pmset")
        self.assertIn("--pmset", skipped.reason)
        self.assertEqual(coffee_awake.amphetamine_fallback_lines(skipped, dry_run=False), ())

        missing = self._ready(installed=False, usable=False, closed_display_sleep_allowed=None)
        self.assertEqual(missing.state, "missing")
        self.assertEqual(coffee_awake.choose_backend(missing)[0], "pmset")
        self.assertIn("using pmset instead", missing.reason)
        missing_lines = coffee_awake.amphetamine_fallback_lines(missing, dry_run=False)
        self.assertTrue(any("not installed" in line for line in missing_lines))
        self.assertTrue(any("App Store" in line for line in missing_lines))
        self.assertTrue(any("Allow system sleep when display is closed" in line for line in missing_lines))
        self.assertTrue(any(line.startswith("Using pmset instead") for line in missing_lines))
        self.assertTrue(any("--pmset" in line for line in missing_lines))
        dry_lines = coffee_awake.amphetamine_fallback_lines(missing, dry_run=True)
        self.assertTrue(any(line.startswith("Would use pmset instead") for line in dry_lines))
        self.assertFalse(any(line.startswith("Using pmset instead") for line in dry_lines))

        unchecked_unknown = self._ready(closed_display_sleep_allowed=None)
        self.assertEqual(unchecked_unknown.state, "not-configured")
        self.assertIn("using pmset instead", unchecked_unknown.reason)
        still_allowed = self._ready(closed_display_sleep_allowed=True)
        self.assertEqual(still_allowed.state, "not-configured")
        configured_lines = coffee_awake.amphetamine_fallback_lines(still_allowed, dry_run=False)
        self.assertTrue(any("not configured for a closed lid" in line for line in configured_lines))
        self.assertTrue(any("will not write it" in line for line in configured_lines))
        self.assertFalse(any("not installed" in line for line in configured_lines))

        broken = self._ready(usable=False)
        self.assertEqual(broken.state, "not-usable")
        self.assertEqual(coffee_awake.choose_backend(broken)[0], "pmset")
        self.assertTrue(
            any("not usable" in line for line in coffee_awake.amphetamine_fallback_lines(broken, dry_run=False))
        )
        self.assertEqual(coffee_awake.amphetamine_fallback_lines(ready, dry_run=False), ())

        failed = coffee_awake.session_failed_readiness()
        self.assertEqual(failed.state, "not-usable")
        self.assertIn("using pmset instead", failed.reason)

        script = coffee_awake.start_session_script()
        self.assertIn("displaySleepAllowed:true", script)
        self.assertIn("interval:hours", script)
        self.assertNotIn("caffeinate", script)
        for module in (coffee_awake, coffee):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotRegex(source, r"""[\[\(]\s*["']caffeinate["']""")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Amphetamine.app"
            app.mkdir()
            self.assertTrue(coffee_awake.amphetamine_installed([app]))
            self.assertFalse(coffee_awake.amphetamine_installed([root / "missing.app"]))
            self.assertFalse(coffee_awake.amphetamine_bundle_usable([app]))
            executable = app / "Contents" / "MacOS" / "Amphetamine"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            self.assertTrue(coffee_awake.amphetamine_bundle_usable([app]))

        source = Path(coffee_awake.__file__).read_text(encoding="utf-8")
        self.assertNotIn("defaults write", source)
        self.assertNotIn("plistlib.dump", source)

    def test_closed_display_preference_is_read_and_container_wins(self) -> None:
        key = coffee_awake.AMPHETAMINE_CLOSED_DISPLAY_SLEEP_KEY
        self.assertIsNone(coffee_awake.parse_closed_display_sleep_allowed(None))
        self.assertIsNone(coffee_awake.parse_closed_display_sleep_allowed({}))
        self.assertIsNone(coffee_awake.parse_closed_display_sleep_allowed({"Other": True}))
        self.assertIs(coffee_awake.parse_closed_display_sleep_allowed({key: False}), False)
        self.assertIs(coffee_awake.parse_closed_display_sleep_allowed({key: True}), True)
        self.assertIs(coffee_awake.parse_closed_display_sleep_allowed({key: 0}), False)
        self.assertIs(coffee_awake.parse_closed_display_sleep_allowed({key: 1}), True)
        self.assertIsNone(coffee_awake.parse_closed_display_sleep_allowed({key: "maybe"}))

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            container = root / "container.plist"
            user_domain = root / "user.plist"
            container.write_bytes(plistlib.dumps({key: True}))
            user_domain.write_bytes(plistlib.dumps({key: False}))
            self.assertIs(
                coffee_awake.read_closed_display_sleep_allowed([container, user_domain]),
                True,
            )
            self.assertIs(
                coffee_awake.read_closed_display_sleep_allowed([root / "missing.plist", user_domain]),
                False,
            )
            self.assertIsNone(coffee_awake.read_closed_display_sleep_allowed([root / "missing.plist"]))

    def test_setup_plan_uses_mas_or_the_public_url_or_the_app(self) -> None:
        missing_mas = coffee_awake.plan_amphetamine_setup(installed=False, mas_on_path=True)
        self.assertEqual(missing_mas.kind, "mas")
        self.assertEqual(
            missing_mas.argv,
            ("mas", "open", coffee_awake.AMPHETAMINE_APP_STORE_ID),
        )
        self.assertNotIn("\n", missing_mas.message)
        self.assertNotIn("defaults", missing_mas.argv)

        missing_url = coffee_awake.plan_amphetamine_setup(installed=False, mas_on_path=False)
        self.assertEqual(missing_url.kind, "url")
        self.assertEqual(missing_url.argv[0], "open")
        self.assertTrue(missing_url.argv[1].startswith("https://apps.apple.com/"))
        self.assertIn(coffee_awake.AMPHETAMINE_APP_STORE_ID, missing_url.argv[1])

        present = coffee_awake.plan_amphetamine_setup(
            installed=True,
            mas_on_path=True,
            closed_display_sleep_allowed=True,
        )
        self.assertEqual(present.kind, "app")
        self.assertEqual(present.argv, ("open", "-a", "Amphetamine"))
        self.assertIn("Allow system sleep when display is closed", present.message)
        self.assertIn("cannot change that checkbox", present.message)

        unknown = coffee_awake.plan_amphetamine_setup(installed=True, mas_on_path=False)
        self.assertEqual(unknown.message, coffee_awake.AMPHETAMINE_SESSION_DEFAULTS_NOTE)

        already = coffee_awake.plan_amphetamine_setup(
            installed=True,
            mas_on_path=True,
            closed_display_sleep_allowed=False,
        )
        self.assertEqual(already.kind, "app")
        self.assertIn("did not change that checkbox", already.message)
        self.assertNotIn("Uncheck", already.message)

        calls: list[list[str]] = []

        def run(argv, interactive=False, timeout=30):
            del interactive, timeout
            calls.append(list(argv))
            if argv and argv[0] == "mas":
                raise coffee_awake.CommandError(argv, 1, "not signed in")
            return coffee_awake.CommandResult(0, "", "")

        used = coffee_awake.run_setup_plan(missing_mas, run=run)
        self.assertEqual(used.kind, "url")
        self.assertEqual(calls[0], ["mas", "open", coffee_awake.AMPHETAMINE_APP_STORE_ID])
        self.assertEqual(calls[1], ["open", coffee_awake.AMPHETAMINE_APP_STORE_URL])

        calls.clear()
        opened = coffee_awake.run_setup_plan(present, run=run)
        self.assertEqual(opened.kind, "app")
        self.assertEqual(calls, [["open", "-a", "Amphetamine"]])

        def fail(argv, interactive=False, timeout=30):
            del interactive, timeout
            raise coffee_awake.CommandError(argv, 1, "no browser")

        with self.assertRaises(coffee_awake.CommandError):
            coffee_awake.run_setup_plan(missing_url, run=fail)

    def _runner(self, value: dict[str, str]):
        calls: list[list[str]] = []

        def run(argv, interactive=False, timeout=30):
            del interactive, timeout
            calls.append(list(argv))
            if argv == ["pmset", "-g"]:
                return coffee_awake.CommandResult(0, f"disablesleep {value['sleep']}\n", "")
            if "disablesleep" in argv and "-a" in argv:
                value["sleep"] = argv[-1]
                return coffee_awake.CommandResult(0, "", "")
            return coffee_awake.CommandResult(0, "", "")

        return run, calls

    def test_real_home_follows_sudo_uid_only_when_root(self) -> None:
        expected = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with (
            patch.object(coffee_awake.os, "geteuid", return_value=0),
            patch.dict(os.environ, {"SUDO_UID": str(os.getuid())}),
        ):
            self.assertEqual(coffee_awake.real_home(), expected)
        with (
            patch.object(coffee_awake.os, "geteuid", return_value=os.getuid()),
            patch.dict(os.environ, {"SUDO_UID": "0"}),
        ):
            self.assertEqual(coffee_awake.real_home(), Path.home())

    def test_pmset_helper_sets_disablesleep_without_sudo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            value = {"sleep": "0"}
            run, calls = self._runner(value)
            alive = {"on": True}

            def pid_alive_fn(_pid: int) -> bool:
                return alive["on"]

            thread = threading.Thread(
                target=coffee_awake.serve_pmset_helper,
                kwargs={
                    "state_dir": state_dir,
                    "watch_pid": os.getpid(),
                    "run": run,
                    "pid_alive_fn": pid_alive_fn,
                    "poll_seconds": 0.05,
                },
                daemon=True,
            )
            thread.start()
            sock = state_dir / "pmset.sock"
            client = coffee_awake.PmsetHelperClient(sock)
            taken = None
            deadline = time.time() + 3
            while time.time() < deadline:
                if sock.exists():
                    try:
                        taken = client.acquire()
                    except coffee_awake.CommandError:
                        taken = None
                        time.sleep(0.05)
                        continue
                    break
                time.sleep(0.02)
            self.assertIsNotNone(taken)
            assert taken is not None
            self.assertTrue(taken.owns)
            self.assertIn("pmset disablesleep 1", taken.note)
            self.assertTrue(any(argv[:4] == ["/usr/bin/pmset", "-a", "disablesleep", "1"] for argv in calls))
            self.assertFalse(any(argv and argv[0] == "sudo" for argv in calls))
            restored = client.release()
            self.assertIn("pmset disablesleep 0", restored.note)
            self.assertFalse((state_dir / "hold.lock").exists())
            client.quit()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_pmset_helper_exit_restores_disablesleep_and_removes_the_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            value = {"sleep": "0"}
            run, calls = self._runner(value)
            # Set after acquire returns. The next poll raises SystemExit
            # instead of the normal pid-death path, so restore has to happen
            # in the helper's finally block.
            stop = {"now": False}

            def pid_alive_fn(_pid: int) -> bool:
                if stop["now"]:
                    raise SystemExit(143)
                return True

            def target() -> None:
                try:
                    coffee_awake.serve_pmset_helper(
                        state_dir,
                        os.getpid(),
                        run=run,
                        pid_alive_fn=pid_alive_fn,
                        poll_seconds=0.05,
                    )
                except SystemExit:
                    return

            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            sock = state_dir / "pmset.sock"
            client = coffee_awake.PmsetHelperClient(sock)
            taken = None
            deadline = time.time() + 3
            while time.time() < deadline:
                if sock.exists():
                    try:
                        taken = client.acquire()
                    except coffee_awake.CommandError:
                        time.sleep(0.05)
                        continue
                    break
                time.sleep(0.02)
            self.assertIsNotNone(taken)
            assert taken is not None
            self.assertTrue(taken.owns)
            self.assertEqual(value["sleep"], "1")
            stop["now"] = True
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(sock.exists())
            self.assertFalse((state_dir / "hold.lock").exists())
            self.assertEqual(value["sleep"], "0")
            self.assertTrue(any(argv[:4] == ["/usr/bin/pmset", "-a", "disablesleep", "0"] for argv in calls))

    def test_pmset_acquire_restarts_a_missing_helper_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)

            def gone() -> None:
                raise coffee_awake.CommandError(
                    ["pmset-helper"],
                    1,
                    "[Errno 2] No such file or directory",
                ) from FileNotFoundError(2, "No such file or directory")

            class GoneClient:
                def acquire(self) -> coffee_awake.PmsetReply:
                    gone()
                    raise AssertionError("unreachable")

            class LiveClient:
                def acquire(self) -> coffee_awake.PmsetReply:
                    return coffee_awake.PmsetReply(True, "pmset disablesleep 1", True)

            starts: list[int] = []

            def start(path: Path, pid: int) -> LiveClient:
                self.assertEqual(path, state_dir)
                self.assertEqual(pid, os.getpid())
                starts.append(pid)
                return LiveClient()

            backend = coffee_awake.PmsetBackend(state_dir, client=GoneClient())  # type: ignore[arg-type]
            with patch.object(coffee_awake, "start_pmset_helper", start):
                note = backend.acquire()
            self.assertEqual(note, "pmset disablesleep 1")
            self.assertTrue(backend.owns_setting)
            self.assertEqual(starts, [os.getpid()])
            self.assertIsInstance(backend.client, LiveClient)

            # A second dead socket in the same acquire starts the helper once, then raises.
            starts.clear()

            def start_still_gone(path: Path, pid: int) -> GoneClient:
                del path
                starts.append(pid)
                return GoneClient()

            backend = coffee_awake.PmsetBackend(state_dir, client=GoneClient())  # type: ignore[arg-type]
            with patch.object(coffee_awake, "start_pmset_helper", start_still_gone):
                with self.assertRaises(coffee_awake.CommandError) as caught:
                    backend.acquire()
            self.assertEqual(starts, [os.getpid()])
            self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
            self.assertFalse(backend.owns_setting)

    def test_pmset_acquire_does_not_restart_helper_after_a_pmset_error(self) -> None:
        class FailedClient:
            def acquire(self) -> coffee_awake.PmsetReply:
                raise coffee_awake.CommandError(["pmset-helper"], 1, "/usr/bin/pmset -a disablesleep 1 failed")

        with tempfile.TemporaryDirectory() as temp:
            backend = coffee_awake.PmsetBackend(Path(temp), client=FailedClient())  # type: ignore[arg-type]
            with patch.object(coffee_awake, "start_pmset_helper") as start:
                with self.assertRaises(coffee_awake.CommandError):
                    backend.acquire()
            start.assert_not_called()
            self.assertFalse(backend.owns_setting)

    def test_helper_and_supervisor_run_this_file(self) -> None:
        script = str(Path(coffee_awake.__file__).resolve())
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with patch.object(coffee_awake.subprocess, "Popen", return_value=object()) as popen:
                coffee_awake.spawn_supervisor(state, 123)
                self.assertEqual(
                    popen.call_args.args[0],
                    [coffee_awake.sys.executable, script, "supervise", str(state), "123"],
                )
                self.assertTrue(popen.call_args.kwargs.get("start_new_session"))
                self.assertNotIn("-m", popen.call_args.args[0])

                popen.reset_mock()
                coffee_awake._spawn_pmset_helper(state, 456)
                expected = [coffee_awake.sys.executable, script, "pmset-helper", str(state), "456"]
                if os.geteuid() != 0:
                    expected = ["sudo", "-n", *expected]
                self.assertEqual(popen.call_args.args[0], expected)
                self.assertNotIn("-m", popen.call_args.args[0])
                self.assertFalse(popen.call_args.kwargs.get("start_new_session", False))

    def test_pmset_acquire_restores_and_does_not_steal_an_existing_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            value = {"sleep": "0"}
            run, calls = self._runner(value)
            spawned: list[tuple[Path, int]] = []
            backend = coffee_awake.PmsetBackend(state_dir, run=run, spawn=lambda path, pid: spawned.append((path, pid)))
            note = backend.acquire()
            self.assertIn("sudo pmset -a disablesleep 1", note)
            self.assertTrue(backend.owns_setting)
            self.assertEqual(len(spawned), 1)
            lock = coffee_awake.read_lock(state_dir / "hold.lock")
            self.assertIsNotNone(lock)
            assert lock is not None
            self.assertEqual(lock.previous_disablesleep, 0)
            self.assertEqual(lock.backend, "pmset")
            self.assertIn(["sudo", "pmset", "-a", "disablesleep", "1"], calls)

            restored = backend.release()
            self.assertIn("pmset disablesleep 0", restored)
            self.assertFalse((state_dir / "hold.lock").exists())
            self.assertFalse(backend.owns_setting)
            self.assertIn(["sudo", "-n", "pmset", "-a", "disablesleep", "0"], calls)

            value["sleep"] = "1"
            calls.clear()
            external = coffee_awake.PmsetBackend(
                state_dir,
                run=run,
                spawn=lambda path, pid: (_ for _ in ()).throw(AssertionError("should not spawn")),
            )
            self.assertIn("already 1", external.acquire())
            self.assertFalse(external.owns_setting)
            self.assertFalse(any(argv[0] == "sudo" and "disablesleep" in argv for argv in calls))

    def test_orphan_lock_restores_only_when_the_owner_is_dead(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            value = {"sleep": "1"}
            run, calls = self._runner(value)
            coffee_awake.write_lock(
                state_dir / "hold.lock",
                coffee_awake.HoldLock(pid=2**30, backend="pmset", previous_disablesleep=0, acquired=True),
            )
            note = coffee_awake.recover_orphan_lock(state_dir, run=run, pid_alive_fn=lambda pid: False)
            self.assertIsNotNone(note)
            assert note is not None
            self.assertIn("pmset disablesleep 0", note)
            self.assertFalse((state_dir / "hold.lock").exists())
            self.assertIn(["sudo", "-n", "pmset", "-a", "disablesleep", "0"], calls)

            coffee_awake.write_lock(
                state_dir / "hold.lock",
                coffee_awake.HoldLock(pid=os.getpid(), backend="pmset", previous_disablesleep=0, acquired=True),
            )
            calls.clear()
            self.assertIsNone(coffee_awake.recover_orphan_lock(state_dir, run=run))
            self.assertTrue((state_dir / "hold.lock").exists())
            self.assertEqual(calls, [])

    def test_supervisor_restores_after_the_parent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            value = {"sleep": "1"}
            run, calls = self._runner(value)
            parent = 2**30
            coffee_awake.write_lock(
                state_dir / "hold.lock",
                coffee_awake.HoldLock(pid=parent, backend="amphetamine", previous_disablesleep=None, acquired=True),
            )
            coffee_awake.supervise(
                state_dir,
                parent,
                sleep=lambda _seconds: None,
                interval=0,
                grace_seconds=0,
                run=run,
            )
            self.assertFalse((state_dir / "hold.lock").exists())
            self.assertTrue(any(argv[:2] == ["osascript", "-e"] for argv in calls))
            self.assertNotIn(["sudo", "-n", "pmset", "-a", "disablesleep", "0"], calls)

    def test_newer_lock_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            run, calls = self._runner({"sleep": "1"})
            coffee_awake.write_lock(
                state_dir / "hold.lock",
                coffee_awake.HoldLock(pid=os.getpid(), backend="pmset", previous_disablesleep=0, acquired=True),
            )
            self.assertIsNone(
                coffee_awake.recover_orphan_lock(
                    state_dir,
                    only_pid=2**30,
                    run=run,
                    pid_alive_fn=lambda pid: False,
                )
            )
            self.assertTrue((state_dir / "hold.lock").exists())
            self.assertEqual(calls, [])


@unittest.skipIf(coffee is None or TOOL_COMMANDS is None, f"Missing dependency: {_IMPORT_ERROR}")
class CoffeeCliTests(unittest.TestCase):
    def test_command_is_registered_like_clock_and_note(self) -> None:
        command = next(item for item in TOOL_COMMANDS if item.name == "coffee")
        self.assertIs(command.app, coffee.app)
        self.assertTrue(command.invoke_without_command)

    def test_help_lists_the_subcommand_and_flags(self) -> None:
        result = CliRunner().invoke(
            coffee.app,
            ["--help"],
            color=False,
            env={"NO_COLOR": "1", "TERM": "dumb"},
        )
        text = _plain((result.output or "") + (result.stderr or ""))
        self.assertEqual(result.exit_code, 0, text)
        self.assertIn("status", text)
        self.assertIn("stop", text)
        self.assertIn("setup", text)
        for flag in ("--idle-seconds", "--poll-seconds", "--pmset", "--allow-battery", "--ac-only", "--dry-run", "--verbose"):
            self.assertIn(flag, text)
        self.assertIn("[default: allow-battery]", text)

    def test_non_macos_refuses_without_changing_sleep(self) -> None:
        # This suite runs on a Mac. Force the non-macOS path so serve() does not loop.
        with patch.object(coffee.sys, "platform", "linux"):
            result = CliRunner().invoke(coffee.app, [])
        text = (result.output or "") + (result.stderr or "")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("macOS", text)
        self.assertIn("Amphetamine", text)
        self.assertIn("pmset", text)

    def test_status_names_the_pmset_fallback_when_amphetamine_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with (
                patch.object(coffee.sys, "platform", "darwin"),
                patch.object(coffee, "coffee_state_dir", return_value=state),
                patch.object(coffee.awake, "amphetamine_installed", return_value=False),
                patch.object(coffee.awake, "amphetamine_bundle_usable", return_value=False),
                patch.object(coffee.awake, "read_closed_display_sleep_allowed", return_value=None),
            ):
                result = CliRunner().invoke(coffee.app, ["status"])
        text = (result.output or "") + (result.stderr or "")
        self.assertEqual(result.exit_code, 0, text)
        self.assertIn("amphetamine: missing", text)
        self.assertIn("default backend: pmset", text)
        self.assertIn("not installed", text)
        self.assertIn("Using pmset instead", text)
        self.assertIn("Allow system sleep when display is closed", text)
        self.assertIn("App Store", text)
        self.assertIn("running: no", text)

    def test_status_says_amphetamine_is_ready_only_when_the_checkbox_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with (
                patch.object(coffee.sys, "platform", "darwin"),
                patch.object(coffee, "coffee_state_dir", return_value=state),
                patch.object(coffee.awake, "amphetamine_installed", return_value=True),
                patch.object(coffee.awake, "amphetamine_bundle_usable", return_value=True),
                patch.object(coffee.awake, "read_closed_display_sleep_allowed", return_value=False),
            ):
                result = CliRunner().invoke(coffee.app, ["status"])
        text = (result.output or "") + (result.stderr or "")
        self.assertEqual(result.exit_code, 0, text)
        self.assertIn("amphetamine: ready", text)
        self.assertIn("default backend: amphetamine", text)
        self.assertNotIn("Using pmset instead", text)
        self.assertIn("does not change that checkbox", text)

    def test_setup_opens_mas_when_amphetamine_is_missing(self) -> None:
        calls: list[list[str]] = []

        def run(argv, interactive=False, timeout=30):
            del interactive, timeout
            calls.append(list(argv))
            return coffee_awake.CommandResult(0, "", "")

        with (
            patch.object(coffee.sys, "platform", "darwin"),
            patch.object(coffee.awake, "amphetamine_installed", return_value=False),
            patch.object(coffee.awake, "amphetamine_bundle_usable", return_value=False),
            patch.object(coffee.shutil, "which", return_value="/usr/local/bin/mas"),
            patch.object(coffee.awake, "default_run", run),
        ):
            result = CliRunner().invoke(coffee.app, ["setup"])
        text = (result.output or "") + (result.stderr or "")
        self.assertEqual(result.exit_code, 0, text)
        self.assertEqual(calls, [["mas", "open", coffee_awake.AMPHETAMINE_APP_STORE_ID]])
        self.assertIn("Using pmset instead", text)
        self.assertIn("Mac App Store", text)

    def test_setup_opens_the_public_url_without_mas(self) -> None:
        calls: list[list[str]] = []

        def run(argv, interactive=False, timeout=30):
            del interactive, timeout
            calls.append(list(argv))
            return coffee_awake.CommandResult(0, "", "")

        with (
            patch.object(coffee.sys, "platform", "darwin"),
            patch.object(coffee.awake, "amphetamine_installed", return_value=False),
            patch.object(coffee.awake, "amphetamine_bundle_usable", return_value=False),
            patch.object(coffee.shutil, "which", return_value=None),
            patch.object(coffee.awake, "default_run", run),
        ):
            result = CliRunner().invoke(coffee.app, ["setup"])
        text = (result.output or "") + (result.stderr or "")
        self.assertEqual(result.exit_code, 0, text)
        self.assertEqual(calls, [["open", coffee_awake.AMPHETAMINE_APP_STORE_URL]])
        self.assertIn("App Store page", text)

    def test_setup_opens_the_app_and_prints_the_checkbox_line(self) -> None:
        calls: list[list[str]] = []

        def run(argv, interactive=False, timeout=30):
            del interactive, timeout
            calls.append(list(argv))
            return coffee_awake.CommandResult(0, "", "")

        with (
            patch.object(coffee.sys, "platform", "darwin"),
            patch.object(coffee.awake, "amphetamine_installed", return_value=True),
            patch.object(coffee.awake, "amphetamine_bundle_usable", return_value=True),
            patch.object(coffee.awake, "read_closed_display_sleep_allowed", return_value=True),
            patch.object(coffee.shutil, "which", return_value="/usr/local/bin/mas"),
            patch.object(coffee.awake, "default_run", run),
        ):
            result = CliRunner().invoke(coffee.app, ["setup"])
        text = (result.output or "") + (result.stderr or "")
        self.assertEqual(result.exit_code, 0, text)
        self.assertEqual(calls, [["open", "-a", "Amphetamine"]])
        self.assertIn(coffee_awake.AMPHETAMINE_SESSION_DEFAULTS_NOTE, text)
        self.assertIn("Using pmset instead", text)
        self.assertNotIn("mas open", " ".join(" ".join(argv) for argv in calls))

    def test_first_run_helper_opens_once_and_stays_quiet_for_dry_run(self) -> None:
        calls: list[list[str]] = []

        def run(argv, interactive=False, timeout=30):
            del interactive, timeout
            calls.append(list(argv))
            return coffee_awake.CommandResult(0, "", "")

        missing = coffee_awake.assess_amphetamine(
            force_pmset=False,
            installed=False,
            usable=False,
            closed_display_sleep_allowed=None,
        )
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            note = coffee._maybe_open_setup_helper(
                readiness=missing,
                state_dir=state,
                dry_run=False,
                interactive=True,
                mas_on_path=True,
                run=run,
            )
            self.assertIsNotNone(note)
            assert note is not None
            self.assertIn("mas", note)
            self.assertEqual(calls, [["mas", "open", coffee_awake.AMPHETAMINE_APP_STORE_ID]])
            self.assertTrue((state / "setup-opened-missing").is_file())
            self.assertIsNone(
                coffee._maybe_open_setup_helper(
                    readiness=missing,
                    state_dir=state,
                    dry_run=False,
                    interactive=True,
                    mas_on_path=True,
                    run=run,
                )
            )
            self.assertEqual(len(calls), 1)

            unconfigured = coffee_awake.assess_amphetamine(
                force_pmset=False,
                installed=True,
                usable=True,
                closed_display_sleep_allowed=None,
            )
            opened = coffee._maybe_open_setup_helper(
                readiness=unconfigured,
                state_dir=state,
                dry_run=False,
                interactive=True,
                mas_on_path=True,
                run=run,
            )
            self.assertEqual(calls[-1], ["open", "-a", "Amphetamine"])
            self.assertIn("Allow system sleep when display is closed", opened or "")
            self.assertIsNone(
                coffee._maybe_open_setup_helper(
                    readiness=missing,
                    state_dir=state,
                    dry_run=True,
                    interactive=True,
                    mas_on_path=False,
                    run=run,
                )
            )
            self.assertIsNone(
                coffee._maybe_open_setup_helper(
                    readiness=missing,
                    state_dir=state / "quiet",
                    dry_run=False,
                    interactive=False,
                    mas_on_path=False,
                    run=run,
                )
            )
            self.assertEqual(len(calls), 2)
            self.assertFalse((state / "quiet" / "setup-opened-missing").exists())

        def fail(argv, interactive=False, timeout=30):
            del argv, interactive, timeout
            raise coffee_awake.CommandError(["open"], 1, "no display")

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with self.assertRaises(coffee_awake.CommandError):
                coffee._maybe_open_setup_helper(
                    readiness=missing,
                    state_dir=state,
                    dry_run=False,
                    interactive=True,
                    mas_on_path=False,
                    run=fail,
                )
            self.assertFalse((state / "setup-opened-missing").exists())

    def test_acquire_backoff_drops_a_queued_acquire(self) -> None:
        queued = [
            coffee_activity.StatusStep("log", "lid opened"),
            coffee_activity.StatusStep("acquire", "", quiet=True),
            coffee_activity.StatusStep("release", ""),
        ]
        waiting = coffee._acquire_steps_for_poll(
            queued,
            now=10,
            retry_at=400,
            want_hold=True,
            held=False,
            quiet=True,
        )
        self.assertEqual([step.kind for step in waiting], ["log", "release"])

        due = coffee._acquire_steps_for_poll(
            [coffee_activity.StatusStep("log", "lid opened")],
            now=400,
            retry_at=400,
            want_hold=True,
            held=False,
            quiet=True,
        )
        self.assertEqual([step.kind for step in due], ["log", "acquire"])
        self.assertTrue(due[-1].quiet)

        kept = coffee._acquire_steps_for_poll(
            queued,
            now=500,
            retry_at=400,
            want_hold=True,
            held=False,
            quiet=False,
        )
        self.assertEqual([step.kind for step in kept], ["log", "acquire", "release"])

        idle = coffee._acquire_steps_for_poll(
            [coffee_activity.StatusStep("log", "lid opened")],
            now=500,
            retry_at=400,
            want_hold=False,
            held=False,
            quiet=False,
        )
        self.assertEqual([step.kind for step in idle], ["log"])


if __name__ == "__main__":
    unittest.main()
