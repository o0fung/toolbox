from __future__ import annotations

import ast
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = ROOT / "pyproject.toml"
TOOLS_PATH = ROOT / "tools"


def _project_dependencies() -> set[str]:
    with PYPROJECT_PATH.open("rb") as handle:
        pyproject = tomllib.load(handle)

    dependencies = pyproject["project"]["dependencies"]
    names: set[str] = set()
    for dependency in dependencies:
        name = dependency.split(";", 1)[0].strip()
        for separator in ("<", ">", "=", "!", "~", "["):
            name = name.split(separator, 1)[0].strip()
        names.add(name.lower().replace("_", "-"))
    return names


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".", 1)[0])
    return imported


class RuntimeDependencyManifestTests(unittest.TestCase):
    def test_direct_click_import_is_declared_runtime_dependency(self) -> None:
        direct_imports = set()
        for module_path in TOOLS_PATH.glob("*.py"):
            direct_imports.update(_top_level_imports(module_path))

        if "click" not in direct_imports:
            self.skipTest("No direct click import found")

        self.assertIn("click", _project_dependencies())


if __name__ == "__main__":
    unittest.main()
