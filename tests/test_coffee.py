"""Tests for lf coffee activity heuristics, hold restore, and status lines."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
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


@unittest.skipIf(coffee_activity is None, f"Missing dependency: {_IMPORT_ERROR}")
class HoldAndStatusTests(unittest.TestCase):
    def test_power_gate(self) -> None:
        busy = coffee_activity.AgentPhase.BUSY
        idle = coffee_activity.AgentPhase.IDLE
        self.assertTrue(coffee_activity.transition_hold(phase=busy, power="ac", allow_battery=False).hold)
        battery = coffee_activity.transition_hold(phase=busy, power="battery", allow_battery=False)
        self.assertFalse(battery.hold)
        self.assertIn("battery", battery.reason)
        self.assertTrue(coffee_activity.transition_hold(phase=busy, power="battery", allow_battery=True).hold)
        self.assertFalse(coffee_activity.transition_hold(phase=busy, power="unknown", allow_battery=False).hold)
        self.assertFalse(coffee_activity.transition_hold(phase=idle, power="ac", allow_battery=True).hold)

    def test_session_lines_follow_lid_agent_and_hold_changes(self) -> None:
        """The verification sequence LF watches in the terminal."""
        home = HOME
        idle = _snap()
        first = coffee_activity.plan_status_events(None, idle, hold=False, home=home)
        self.assertEqual(
            _kinds(first),
            [
                ("log", "lid open (best-effort; AppleClamshellState=No)"),
                ("log", "no local agent is busy"),
            ],
        )
        self.assertEqual(coffee_activity.plan_status_events(idle, idle, hold=False, home=home), [])

        working = _agent()
        busy = _snap(
            agents=(working,),
            phase=coffee_activity.AgentPhase.BUSY,
            reason="local agent busy: Fix cache",
            want_hold=True,
        )
        started = coffee_activity.plan_status_events(idle, busy, hold=False, home=home)
        self.assertEqual(
            _kinds(started),
            [
                ("log", "agent busy: Fix cache (id abc, workspace ~/src/api)"),
                ("acquire", ""),
            ],
        )
        self.assertEqual(coffee_activity.plan_status_events(busy, busy, hold=True, home=home), [])

        closed = _snap(
            lid="closed",
            lid_detail="best-effort; AppleClamshellState=Yes",
            agents=(working,),
            phase=coffee_activity.AgentPhase.BUSY,
            reason="local agent busy: Fix cache",
            want_hold=True,
        )
        lid_close = coffee_activity.plan_status_events(busy, closed, hold=True, home=home)
        self.assertEqual(
            _kinds(lid_close),
            [
                ("log", "lid closed (best-effort; AppleClamshellState=Yes)"),
                ("log", "agent busy: Fix cache (id abc, workspace ~/src/api)"),
            ],
        )

        done = _snap(
            lid="closed",
            lid_detail="best-effort; AppleClamshellState=Yes",
        )
        finished = coffee_activity.plan_status_events(closed, done, hold=True, home=home)
        self.assertEqual(
            _kinds(finished),
            [
                ("log", "agent finished: Fix cache (id abc, workspace ~/src/api)"),
                ("log", "all local agents idle; releasing sleep hold"),
                ("release", ""),
            ],
        )

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
        self.assertEqual(_kinds(steps), [("log", "agent finished: One (id aaa, workspace ~/src/api)")])

    def test_battery_announces_the_agent_without_acquiring(self) -> None:
        busy = _snap(
            agents=(_agent(),),
            phase=coffee_activity.AgentPhase.BUSY,
            power="battery",
            want_hold=False,
            block_reason="on battery; pass --allow-battery to hold anyway",
        )
        steps = coffee_activity.plan_status_events(None, busy, hold=False, home=HOME)
        texts = [step.text for step in steps if step.kind == "log"]
        self.assertIn("agent busy: Fix cache (id abc, workspace ~/src/api)", texts)
        self.assertTrue(any(text.startswith("not holding sleep:") for text in texts))
        self.assertNotIn("acquire", [step.kind for step in steps])
        self.assertEqual(coffee_activity.plan_status_events(busy, busy, hold=False, home=HOME), [])

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
                "sleeping / hold released (fake-release)",
            ],
        )
        self.assertEqual(backend.calls, ["release"])
        self.assertFalse(controller.held)
        self.assertEqual(coffee.release_line(""), "sleeping / hold released")


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

    def test_backend_choice_and_amphetamine_script(self) -> None:
        self.assertEqual(
            coffee_awake.choose_backend(force_pmset=False, amphetamine_present=True),
            ("amphetamine", "Amphetamine is installed"),
        )
        self.assertEqual(coffee_awake.choose_backend(force_pmset=True, amphetamine_present=True)[0], "pmset")
        self.assertEqual(coffee_awake.choose_backend(force_pmset=False, amphetamine_present=False)[0], "pmset")
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

    def _runner(self, value: dict[str, str]):
        calls: list[list[str]] = []

        def run(argv, interactive=False, timeout=30):
            del interactive, timeout
            calls.append(list(argv))
            if argv == ["pmset", "-g"]:
                return coffee_awake.CommandResult(0, f"disablesleep {value['sleep']}\n", "")
            if argv and argv[0] == "sudo" and "disablesleep" in argv:
                value["sleep"] = argv[-1]
                return coffee_awake.CommandResult(0, "", "")
            return coffee_awake.CommandResult(0, "", "")

        return run, calls

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
        result = CliRunner().invoke(coffee.app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("status", result.output)
        self.assertIn("stop", result.output)
        for flag in ("--idle-seconds", "--poll-seconds", "--pmset", "--allow-battery", "--dry-run", "--verbose"):
            self.assertIn(flag, result.output)

    def test_non_macos_refuses_without_changing_sleep(self) -> None:
        result = CliRunner().invoke(coffee.app, [])
        text = (result.output or "") + (result.stderr or "")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("macOS", text)
        self.assertIn("Amphetamine", text)
        self.assertIn("pmset", text)


if __name__ == "__main__":
    unittest.main()
