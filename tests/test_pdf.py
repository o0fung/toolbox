from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from tools import pdf
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    pdf = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(pdf is None, f"Missing dependency: {_IMPORT_ERROR}")
class PdfToolTests(unittest.TestCase):
    def test_find_ghostscript_prefers_unix_binary_name(self) -> None:
        paths = {
            "gs": "/usr/bin/gs",
            "gswin64c": "C:/Program Files/gs/gswin64c.exe",
        }

        with patch.object(pdf.shutil, "which", side_effect=lambda binary: paths.get(binary)):
            self.assertEqual(pdf._find_ghostscript_executable(), "/usr/bin/gs")

    def test_find_ghostscript_accepts_windows_console_binary(self) -> None:
        paths = {"gswin64c": "C:/Program Files/gs/gswin64c.exe"}

        with patch.object(pdf.shutil, "which", side_effect=lambda binary: paths.get(binary)):
            self.assertEqual(
                pdf._find_ghostscript_executable(),
                "C:/Program Files/gs/gswin64c.exe",
            )

    def test_ensure_ghostscript_rechecks_windows_names_after_install_prompt(self) -> None:
        with patch.object(
            pdf,
            "_find_ghostscript_executable",
            side_effect=[None, "C:/gs/gswin32c.exe"],
        ):
            with patch.object(pdf, "ensure_binary_or_prompt_install", return_value=None):
                self.assertEqual(pdf._ensure_ghostscript_executable(), "C:/gs/gswin32c.exe")

    def test_resolve_output_default(self) -> None:
        input_path = Path("/tmp/sample.pdf")
        resolved = pdf._resolve_output_path(input_path, None)
        self.assertEqual(str(resolved), "/tmp/sample_compressed.pdf")

    def test_resolve_output_adds_pdf_extension(self) -> None:
        input_path = Path("/tmp/sample.pdf")
        resolved = pdf._resolve_output_path(input_path, Path("/tmp/out/report"))
        self.assertEqual(str(resolved), "/tmp/out/report.pdf")

    def test_human_size_units(self) -> None:
        self.assertEqual(pdf._human_size(512), "512.0B")
        self.assertEqual(pdf._human_size(2048), "2.0KB")

if __name__ == "__main__":
    unittest.main()
