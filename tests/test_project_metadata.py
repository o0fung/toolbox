from __future__ import annotations

import unittest
from importlib.metadata import PackageNotFoundError, requires


class ProjectMetadataTests(unittest.TestCase):
    def test_declares_direct_click_runtime_dependency(self) -> None:
        try:
            dependency_specs = requires("lf-toolbox")
        except PackageNotFoundError as exc:
            self.skipTest(f"lf-toolbox package metadata is not installed: {exc}")

        normalized_specs = [
            spec.split(";", 1)[0].strip().lower()
            for spec in (dependency_specs or [])
        ]
        self.assertTrue(
            any(spec.startswith("click") for spec in normalized_specs),
            "tools.plot imports click directly, so fresh installs must include click",
        )


if __name__ == "__main__":
    unittest.main()
