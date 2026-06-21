from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from tools import youtube
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    youtube = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(youtube is None, f"Missing dependency: {_IMPORT_ERROR}")
class YoutubeToolTests(unittest.TestCase):
    def test_build_ydl_options_subtitle_only_skips_media_download(self) -> None:
        opts = youtube._build_ydl_options(
            output_dir=Path("/tmp/out"),
            video=False,
            audio=False,
            subtitle=True,
            fmt=None,
        )

        self.assertTrue(opts["skip_download"])
        self.assertTrue(opts["writesubtitles"])
        self.assertTrue(opts["writeautomaticsub"])

    def test_download_with_fallback_keeps_download_phase_for_subtitle_only(self) -> None:
        calls: list[tuple[str, dict[str, object], bool]] = []

        def fake_extract_info(url: str, ydl_opts: dict[str, object], download: bool) -> dict[str, object]:
            calls.append((url, ydl_opts, download))
            return {"id": "video-id"}

        with patch.object(youtube, "_extract_info", side_effect=fake_extract_info):
            result = youtube._download_with_fallback(
                url="https://example.com/watch?v=abc",
                ydl_opts={"skip_download": True},
                format_candidates=("best",),
            )

        self.assertEqual(result, {"id": "video-id"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["format"], "best")
        self.assertTrue(calls[0][2])


if __name__ == "__main__":
    unittest.main()
