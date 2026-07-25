from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

try:
    import typer
    from typer.testing import CliRunner
    from tools import note
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    typer = None  # type: ignore[assignment]
    CliRunner = None  # type: ignore[assignment]
    note = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(note is None, f"Missing dependency: {_IMPORT_ERROR}")
class NoteToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp_dir.name)
        self.notes_root = self.temp_path / "Documents" / "note"
        self.config_path = self.temp_path / "note.defaults.json"
        self._original_config_path = note._DEFAULT_NOTE_CONFIG_PATH
        note._DEFAULT_NOTE_CONFIG_PATH = str(self.config_path)
        self.addCleanup(self._temp_dir.cleanup)

    def tearDown(self) -> None:
        note._DEFAULT_NOTE_CONFIG_PATH = self._original_config_path

    def _write_config(self, **overrides: object) -> None:
        payload = note._default_note_config_payload()
        payload["notes_dir"] = str(self.notes_root)
        payload.update(overrides)
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_ensure_config_creates_loadable_defaults_without_overwriting(self) -> None:
        self.assertTrue(note._ensure_note_config_file(str(self.config_path)))
        self.assertFalse(note._ensure_note_config_file(str(self.config_path)))

        loaded = note._load_note_config(str(self.config_path))
        self.assertEqual(loaded["notes_dir"], "~/Documents/note")
        self.assertIsNone(loaded["editor"])
        self.assertTrue(loaded["add_title_heading"])

    def test_load_config_merges_partial_values_and_rejects_unknown_keys(self) -> None:
        self.config_path.write_text(json.dumps({"editor": "code --wait"}), encoding="utf-8")
        loaded = note._load_note_config(str(self.config_path))
        self.assertEqual(loaded["editor"], "code --wait")
        self.assertEqual(loaded["notes_dir"], "~/Documents/note")

        self.config_path.write_text(json.dumps({"unknown": True}), encoding="utf-8")
        with self.assertRaises(typer.BadParameter):
            note._load_note_config(str(self.config_path))

    def test_normalize_title_is_cross_platform_safe(self) -> None:
        self.assertEqual(note._normalize_title("  API / Design: Review?  "), "api-design-review")
        self.assertEqual(note._normalize_title("會議 記錄"), "會議-記錄")
        with self.assertRaises(typer.BadParameter):
            note._normalize_title("***")

    def test_create_note_adds_heading_and_avoids_overwrite(self) -> None:
        self.notes_root.mkdir(parents=True)
        now = datetime(2026, 7, 25, 16, 0, 0)

        first = note._create_note(self.notes_root, "Project Ideas", True, now)
        second = note._create_note(self.notes_root, "Project Ideas", True, now)

        self.assertEqual(first.name, "20260725-160000_project-ideas.md")
        self.assertEqual(second.name, "20260725-160000_project-ideas-2.md")
        self.assertEqual(first.read_text(encoding="utf-8"), "# Project Ideas\n\n")

    def test_cli_creates_note_in_nested_subfolder_and_opens_it(self) -> None:
        self._write_config(editor="test-editor")
        opened: list[Path] = []

        with patch("tools.note._open_in_editor", side_effect=lambda path, _editor: opened.append(path)):
            result = CliRunner().invoke(note.app, ["API", "decision", "-f", "work/backend"])

        self.assertEqual(result.exit_code, 0, result.output)
        created = list((self.notes_root / "work" / "backend").glob("*.md"))
        self.assertEqual(len(created), 1)
        self.assertRegex(created[0].name, r"^\d{8}-\d{6}_api-decision\.md$")
        self.assertEqual(created[0].read_text(encoding="utf-8"), "# API decision\n\n")
        self.assertEqual(opened, created)

    def test_cli_respects_disabled_title_heading(self) -> None:
        self._write_config(add_title_heading=False)

        with patch("tools.note._open_in_editor"):
            result = CliRunner().invoke(note.app, ["Empty", "body"])

        self.assertEqual(result.exit_code, 0, result.output)
        created = next(self.notes_root.glob("*.md"))
        self.assertEqual(created.read_text(encoding="utf-8"), "")

    def test_recursive_list_is_newest_first_and_filterable(self) -> None:
        self._write_config()
        (self.notes_root / "work").mkdir(parents=True)
        (self.notes_root / "personal").mkdir()
        (self.notes_root / "work" / "20260725-160000_api-review.md").write_text("", encoding="utf-8")
        (self.notes_root / "personal" / "20260724-090000_shopping.md").write_text("", encoding="utf-8")

        all_result = CliRunner().invoke(note.app, ["--list"])
        filtered_result = CliRunner().invoke(note.app, ["--list", "--match", "api"])

        self.assertEqual(all_result.exit_code, 0, all_result.output)
        self.assertEqual(
            all_result.output.strip().splitlines(),
            [
                "work/20260725-160000_api-review.md",
                "personal/20260724-090000_shopping.md",
            ],
        )
        self.assertEqual(filtered_result.exit_code, 0, filtered_result.output)
        self.assertEqual(filtered_result.output.strip(), "work/20260725-160000_api-review.md")

    def test_list_on_missing_main_folder_is_empty(self) -> None:
        self._write_config()
        result = CliRunner().invoke(note.app, ["--list"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.strip(), "No notes found.")
        self.assertFalse(self.notes_root.exists())

    def test_open_uses_unique_recursive_partial_match(self) -> None:
        self._write_config()
        target = self.notes_root / "work" / "20260725-160000_api-decision.md"
        target.parent.mkdir(parents=True)
        target.write_text("", encoding="utf-8")
        opened: list[Path] = []

        with patch("tools.note._open_in_editor", side_effect=lambda path, _editor: opened.append(path)):
            result = CliRunner().invoke(note.app, ["--open", "api-decision"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(opened, [target])

    def test_open_reports_ambiguous_matches(self) -> None:
        self._write_config()
        for folder, filename in (
            ("work", "20260725-160000_review.md"),
            ("personal", "20260724-160000_review.md"),
        ):
            target = self.notes_root / folder / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")

        result = CliRunner().invoke(note.app, ["--open", "review"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Multiple notes match 'review'", result.output)
        self.assertIn("work/20260725-160000_review.md", result.output)
        self.assertIn("personal/20260724-160000_review.md", result.output)

    def test_subfolder_scope_disambiguates_open(self) -> None:
        self._write_config()
        expected = self.notes_root / "work" / "20260725-160000_review.md"
        other = self.notes_root / "personal" / "20260724-160000_review.md"
        expected.parent.mkdir(parents=True)
        other.parent.mkdir(parents=True)
        expected.write_text("", encoding="utf-8")
        other.write_text("", encoding="utf-8")

        with patch("tools.note._open_in_editor") as open_editor:
            result = CliRunner().invoke(note.app, ["--open", "review", "-f", "work"])

        self.assertEqual(result.exit_code, 0, result.output)
        open_editor.assert_called_once_with(expected, None)

    def test_subfolder_rejects_absolute_and_parent_traversal(self) -> None:
        self._write_config()
        absolute = CliRunner().invoke(note.app, ["Title", "-f", str(self.temp_path)])
        traversal = CliRunner().invoke(note.app, ["Title", "-f", "../outside"])

        self.assertNotEqual(absolute.exit_code, 0)
        self.assertIn("must be relative", absolute.output)
        self.assertNotEqual(traversal.exit_code, 0)
        self.assertIn("must not contain", traversal.output)

    def test_browse_creates_selected_folder(self) -> None:
        self._write_config()
        browsed: list[Path] = []

        with patch("tools.note._browse_directory", side_effect=lambda path: browsed.append(path)):
            result = CliRunner().invoke(note.app, ["--browse", "-f", "work/projects"])

        expected = self.notes_root / "work" / "projects"
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(expected.is_dir())
        self.assertEqual(browsed, [expected])

    def test_config_action_creates_and_opens_config(self) -> None:
        opened: list[Path] = []
        with patch("tools.note._open_in_editor", side_effect=lambda path, _editor: opened.append(path)):
            result = CliRunner().invoke(note.app, ["--config"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(self.config_path.is_file())
        self.assertEqual(opened, [self.config_path])

    def test_modes_and_match_are_validated_before_side_effects(self) -> None:
        self._write_config()
        combined = CliRunner().invoke(note.app, ["Title", "--list"])
        misplaced_match = CliRunner().invoke(note.app, ["Title", "--match", "x"])
        empty = CliRunner().invoke(note.app, [])

        self.assertNotEqual(combined.exit_code, 0)
        self.assertIn("Choose only one action", combined.output)
        self.assertNotEqual(misplaced_match.exit_code, 0)
        self.assertIn("--match may only be used with --list", misplaced_match.output)
        self.assertNotEqual(empty.exit_code, 0)
        self.assertIn("Provide a note TITLE", empty.output)
        self.assertFalse(self.notes_root.exists())

    def test_editor_resolution_prefers_config_then_environment(self) -> None:
        with patch.dict(os.environ, {"VISUAL": "code --wait", "EDITOR": "vim"}, clear=False):
            self.assertEqual(note._resolve_editor("nano -w"), ["nano", "-w"])
            self.assertEqual(note._resolve_editor(None), ["code", "--wait"])


if __name__ == "__main__":
    unittest.main()
