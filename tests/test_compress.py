from __future__ import annotations

import unittest
from pathlib import Path

try:
    from tools import compress
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    compress = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(compress is None, f"Missing dependency: {_IMPORT_ERROR}")
class CompressToolTests(unittest.TestCase):
    def test_resolve_pdf_output_default(self) -> None:
        input_path = Path("/tmp/sample.pdf")
        resolved = compress._resolve_pdf_output_path(input_path, None)
        self.assertEqual(str(resolved), "/tmp/sample_compressed.pdf")

    def test_resolve_pdf_output_adds_pdf_extension(self) -> None:
        input_path = Path("/tmp/sample.pdf")
        resolved = compress._resolve_pdf_output_path(input_path, Path("/tmp/out/report"))
        self.assertEqual(str(resolved), "/tmp/out/report.pdf")

    def test_resolve_video_output_default(self) -> None:
        input_path = Path("/tmp/clip.mov")
        resolved = compress._resolve_video_output_path(input_path, None)
        self.assertEqual(str(resolved), "/tmp/clip_compressed.mp4")

    def test_resolve_video_output_adds_mp4_extension(self) -> None:
        input_path = Path("/tmp/clip.mov")
        resolved = compress._resolve_video_output_path(input_path, Path("/tmp/out/clip-small"))
        self.assertEqual(str(resolved), "/tmp/out/clip-small.mp4")

    def test_build_video_command_uses_mp4_compression_defaults(self) -> None:
        command = compress._build_video_command(
            ffmpeg_exe="/usr/bin/ffmpeg",
            input_path=Path("/tmp/input.mov"),
            output_path=Path("/tmp/output.mp4"),
            width=1280,
            crf=28,
            preset="slow",
            overwrite=False,
        )

        self.assertEqual(
            command,
            [
                "/usr/bin/ffmpeg",
                "-n",
                "-i",
                "/tmp/input.mov",
                "-vf",
                "scale=1280:-2",
                "-c:v",
                "libx264",
                "-crf",
                "28",
                "-preset",
                "slow",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "/tmp/output.mp4",
            ],
        )

    def test_human_size_units(self) -> None:
        self.assertEqual(compress._human_size(512), "512.0B")
        self.assertEqual(compress._human_size(2048), "2.0KB")


if __name__ == "__main__":
    unittest.main()
