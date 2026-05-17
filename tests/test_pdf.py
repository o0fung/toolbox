from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

try:
    from tools import pdf
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    pdf = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(pdf is None, f"Missing dependency: {_IMPORT_ERROR}")
class PdfToolTests(unittest.TestCase):
    def test_resolve_output_default(self) -> None:
        input_path = Path("/tmp/sample.pdf")
        resolved = pdf._resolve_output_path(input_path, None)
        self.assertEqual(str(resolved), "/tmp/sample_compressed.pdf")

    def test_resolve_output_adds_pdf_extension(self) -> None:
        input_path = Path("/tmp/sample.pdf")
        resolved = pdf._resolve_output_path(input_path, Path("/tmp/out/report"))
        self.assertEqual(str(resolved), "/tmp/out/report.pdf")

    def test_paths_refer_to_same_file_detects_hard_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_path = Path(tmp_dir) / "input.pdf"
            linked_output = Path(tmp_dir) / "linked-output.pdf"
            input_path.write_bytes(b"%PDF-1.4\n")
            try:
                os.link(input_path, linked_output)
            except (AttributeError, NotImplementedError, OSError) as exc:
                self.skipTest(f"hard links are unavailable: {exc}")

            self.assertTrue(pdf._paths_refer_to_same_file(input_path.resolve(), linked_output))

    def test_human_size_units(self) -> None:
        self.assertEqual(pdf._human_size(512), "512.0B")
        self.assertEqual(pdf._human_size(2048), "2.0KB")

if __name__ == "__main__":
    unittest.main()
