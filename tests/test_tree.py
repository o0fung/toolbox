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

    def test_load_module_creates_missing_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            callback = tree._load_module(root, "custom_script", "custom_func")
            self.assertTrue(callable(callback))

            module_file = root / "custom_script.py"
            self.assertTrue(module_file.exists())
            text = module_file.read_text(encoding="utf-8")
            self.assertIn("def custom_func(filepath: str):", text)


if __name__ == "__main__":
    unittest.main()
