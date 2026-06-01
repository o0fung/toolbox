from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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

    def test_resolve_output_existing_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "sample.pdf"
            out_dir = tmp_path / "compressed"
            out_dir.mkdir()

            resolved = pdf._resolve_output_path(input_path, out_dir)

            self.assertEqual(resolved, out_dir / "sample_compressed.pdf")

    def test_human_size_units(self) -> None:
        self.assertEqual(pdf._human_size(512), "512.0B")
        self.assertEqual(pdf._human_size(2048), "2.0KB")

if __name__ == "__main__":
    unittest.main()
