from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from rich.console import Console
    from tools import tree
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    Console = None  # type: ignore[assignment]
    tree = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _render_text(tree_obj) -> str:
    assert Console is not None
    console = Console(record=True, width=120)
    console.print(tree_obj)
    return console.export_text()


@unittest.skipIf(tree is None, f"Missing dependency: {_IMPORT_ERROR}")
class TreeToolTests(unittest.TestCase):
    def test_build_tree_handles_file_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            file_path = root / "note.txt"
            file_path.write_text("hello", encoding="utf-8")

            tree_obj = tree._build_tree(
                target=file_path,
                max_depth=1,
                skip_hidden=False,
                callback=None,
            )
            output = _render_text(tree_obj)
            self.assertIn("note.txt", output)

    def test_depth_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            nested = root / "a" / "b"
            nested.mkdir(parents=True, exist_ok=True)
            (nested / "deep.txt").write_text("x", encoding="utf-8")

            limited = tree._build_tree(
                target=root,
                max_depth=1,
                skip_hidden=False,
                callback=None,
            )
            limited_output = _render_text(limited)
            self.assertIn("a", limited_output)
            self.assertNotIn("deep.txt", limited_output)

            unlimited = tree._build_tree(
                target=root,
                max_depth=0,
                skip_hidden=False,
                callback=None,
            )
            unlimited_output = _render_text(unlimited)
            self.assertIn("deep.txt", unlimited_output)

    def test_build_tree_does_not_follow_directory_symlink_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "note.txt").write_text("hello", encoding="utf-8")
            loop = root / "loop"
            try:
                loop.symlink_to(root, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")

            tree_obj = tree._build_tree(
                target=root,
                max_depth=0,
                skip_hidden=False,
                callback=None,
            )

            output = _render_text(tree_obj)
            self.assertIn("loop", output)
            self.assertEqual(output.count("loop"), 1)

    def test_load_module_creates_missing_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            callback = tree._load_module(root, "custom_script", "custom_func")
            self.assertTrue(callable(callback))

            module_file = root / "custom_script.py"
            self.assertTrue(module_file.exists())
            text = module_file.read_text(encoding="utf-8")
            self.assertIn("def custom_func(filepath: str):", text)

    def test_load_module_rejects_path_traversal_module_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            root = base / "target"
            root.mkdir()
            outside_module = base / "victim.py"
            original = "VALUE = 1\n"
            outside_module.write_text(original, encoding="utf-8")

            with self.assertRaises(tree.typer.BadParameter):
                tree._load_module(root, "../victim", "custom_func")

            self.assertEqual(outside_module.read_text(encoding="utf-8"), original)

    def test_load_module_rejects_invalid_function_name_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            with self.assertRaises(tree.typer.BadParameter):
                tree._load_module(root, "custom_script", "bad-name")

            self.assertFalse((root / "custom_script.py").exists())


if __name__ == "__main__":
    unittest.main()
