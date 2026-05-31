from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import typer

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

    def test_human_size_units(self) -> None:
        self.assertEqual(pdf._human_size(512), "512.0B")
        self.assertEqual(pdf._human_size(2048), "2.0KB")

    def test_failed_compression_preserves_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_path = root / "input.pdf"
            output_path = root / "output.pdf"
            input_path.write_bytes(b"%PDF-1.4 input")
            output_path.write_bytes(b"%PDF existing output")

            def fail_after_partial_write(command, **_kwargs):
                output_arg = next(arg for arg in command if arg.startswith("-sOutputFile="))
                temp_output_path = Path(output_arg.split("=", 1)[1])
                temp_output_path.write_bytes(b"partial output")
                raise subprocess.CalledProcessError(1, command, stderr="boom")

            with (
                mock.patch.object(pdf, "ensure_binary_or_prompt_install", return_value="gs"),
                mock.patch.object(pdf.subprocess, "run", side_effect=fail_after_partial_write),
                mock.patch.object(pdf, "info"),
            ):
                with self.assertRaises(typer.Exit):
                    pdf.pdf(input_pdf=input_path, out=output_path, quality="ebook")

            self.assertEqual(output_path.read_bytes(), b"%PDF existing output")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["input.pdf", "output.pdf"])

    def test_successful_compression_replaces_output_from_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            input_path = root / "input.pdf"
            output_path = root / "output.pdf"
            input_path.write_bytes(b"%PDF-1.4 input")
            output_path.write_bytes(b"%PDF old output")

            def write_compressed_output(command, **_kwargs):
                output_arg = next(arg for arg in command if arg.startswith("-sOutputFile="))
                temp_output_path = Path(output_arg.split("=", 1)[1])
                self.assertNotEqual(temp_output_path, output_path)
                temp_output_path.write_bytes(b"%PDF compressed output")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with (
                mock.patch.object(pdf, "ensure_binary_or_prompt_install", return_value="gs"),
                mock.patch.object(pdf.subprocess, "run", side_effect=write_compressed_output),
                mock.patch.object(pdf, "info"),
            ):
                pdf.pdf(input_pdf=input_path, out=output_path, quality="ebook")

            self.assertEqual(output_path.read_bytes(), b"%PDF compressed output")

if __name__ == "__main__":
    unittest.main()
