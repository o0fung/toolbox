from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

try:
    import typer
    from typer.testing import CliRunner
    from tools import plot
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    typer = None  # type: ignore[assignment]
    CliRunner = None  # type: ignore[assignment]
    plot = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(plot is None, f"Missing dependency: {_IMPORT_ERROR}")
class PlotToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original_config_path = plot._DEFAULT_PLOT_CONFIG_PATH
        plot._DEFAULT_PLOT_CONFIG_PATH = os.path.join(self._temp_dir.name, "plot.defaults.json")
        self.addCleanup(self._temp_dir.cleanup)

    def tearDown(self) -> None:
        plot._DEFAULT_PLOT_CONFIG_PATH = self._original_config_path

    def _write_temp_json(self, payload: object) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump(payload, handle)
            handle.flush()
            return handle.name
        finally:
            handle.close()

    def test_parse_xlim_accepts_float_and_open_bounds(self) -> None:
        self.assertEqual(plot._parse_xlim("1.5,2.5"), (1.5, 2.5))
        self.assertEqual(plot._parse_xlim(",2"), (None, 2.0))
        self.assertEqual(plot._parse_xlim("2,"), (2.0, None))
        self.assertEqual(plot._parse_xlim("3:4"), (3.0, 4.0))

    def test_parse_xlim_rejects_reversed_bounds(self) -> None:
        with self.assertRaises(typer.BadParameter):
            plot._parse_xlim("5,1")

    def test_filter_scaled_xlim_clamps_to_data_bounds(self) -> None:
        xs = [0.0, 1.0, 2.0, 3.0]
        ycols = [(1, "signal", [10.0, 11.0, 12.0, 13.0])]

        filtered_xs, filtered_ycols, applied_xlim, data_xlim = plot._filter_scaled_xlim(xs, ycols, (-10.0, 10.0))

        self.assertEqual(filtered_xs, [0.0, 1.0, 2.0, 3.0])
        self.assertEqual(filtered_ycols[0][2], [10.0, 11.0, 12.0, 13.0])
        self.assertEqual(applied_xlim, (0.0, 3.0))
        self.assertEqual(data_xlim, (0.0, 3.0))

    def test_filter_scaled_xlim_clamps_out_of_range_to_endpoint(self) -> None:
        xs = [0.0, 2.0, 4.0]
        ycols = [(1, "signal", [10.0, 20.0, 30.0])]

        filtered_xs, filtered_ycols, applied_xlim, data_xlim = plot._filter_scaled_xlim(xs, ycols, (100.0, 200.0))

        self.assertEqual(filtered_xs, [4.0])
        self.assertEqual(filtered_ycols[0][2], [30.0])
        self.assertEqual(applied_xlim, (4.0, 4.0))
        self.assertEqual(data_xlim, (0.0, 4.0))

    def test_filter_scaled_xlim_uses_scaled_x_values(self) -> None:
        raw_xs = [1000.0, 1100.0, 1200.0]
        scaled_xs = [x * 0.02 for x in raw_xs]
        ycols = [(1, "signal", [1.0, 2.0, 3.0])]

        filtered_xs, filtered_ycols, applied_xlim, _ = plot._filter_scaled_xlim(scaled_xs, ycols, (21.0, 23.0))

        self.assertEqual(filtered_xs, [22.0])
        self.assertEqual(filtered_ycols[0][2], [2.0])
        self.assertEqual(applied_xlim, (21.0, 23.0))

    def test_load_plot_config_accepts_valid_json(self) -> None:
        path = self._write_temp_json({"scale": 0.02, "xcol": 6, "ycols": [20, 21, "22"], "xlim": "10,20"})
        try:
            loaded = plot._load_plot_config(path)
        finally:
            os.unlink(path)

        self.assertEqual(loaded["scale"], 0.02)
        self.assertEqual(loaded["xcol"], "6")
        self.assertEqual(loaded["ycols"], "20,21,22")
        self.assertEqual(loaded["xlim"], "10,20")

    def test_load_plot_config_rejects_missing_file(self) -> None:
        with self.assertRaises(typer.BadParameter):
            plot._load_plot_config("/tmp/this_file_should_not_exist_plot.json")

    def test_load_plot_config_rejects_invalid_json(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        try:
            handle.write("{invalid-json")
            handle.flush()
            bad_path = handle.name
        finally:
            handle.close()
        try:
            with self.assertRaises(typer.BadParameter):
                plot._load_plot_config(bad_path)
        finally:
            os.unlink(bad_path)

    def test_load_plot_config_rejects_unknown_keys(self) -> None:
        path = self._write_temp_json({"scale": 1.0, "unexpected_key": 1})
        try:
            with self.assertRaises(typer.BadParameter):
                plot._load_plot_config(path)
        finally:
            os.unlink(path)

    def test_load_plot_config_rejects_wrong_types(self) -> None:
        path = self._write_temp_json({"scale": "fast"})
        try:
            with self.assertRaises(typer.BadParameter):
                plot._load_plot_config(path)
        finally:
            os.unlink(path)

    def test_merge_plot_options_cli_overrides_config(self) -> None:
        cli_values = {
            "delimiter": None,
            "title": None,
            "scale": 1.0,
            "export": False,
            "out_path": None,
            "xcol": None,
            "ycols": None,
            "xlim": None,
            "weight": 1.0,
            "points_only": False,
        }
        config_values = {"scale": 0.02, "xcol": "6", "ycols": "20,21,22"}
        merged = plot._merge_plot_options(cli_values, config_values, {"scale"})

        self.assertEqual(merged["scale"], 1.0)
        self.assertEqual(merged["xcol"], "6")
        self.assertEqual(merged["ycols"], "20,21,22")

    def test_ensure_plot_config_file_creates_default_payload(self) -> None:
        created = plot._ensure_plot_config_file(plot._DEFAULT_PLOT_CONFIG_PATH)
        self.assertTrue(created)

        with open(plot._DEFAULT_PLOT_CONFIG_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        self.assertEqual(loaded["scale"], 1.0)
        self.assertIsNone(loaded["ycols"])

    def test_ensure_plot_config_file_does_not_overwrite_existing_file(self) -> None:
        with open(plot._DEFAULT_PLOT_CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump({"scale": 0.02}, handle)
        created = plot._ensure_plot_config_file(plot._DEFAULT_PLOT_CONFIG_PATH)
        self.assertFalse(created)

        with open(plot._DEFAULT_PLOT_CONFIG_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        self.assertEqual(loaded, {"scale": 0.02})

    def test_config_show_creates_opens_and_exits_without_csv(self) -> None:
        runner = CliRunner()
        opened_paths: list[str] = []

        with patch("tools.plot._open_path_for_edit", side_effect=lambda p: opened_paths.append(p)):
            result = runner.invoke(plot.app, ["--config-show"])

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(os.path.isfile(plot._DEFAULT_PLOT_CONFIG_PATH))
        self.assertEqual(opened_paths, [plot._DEFAULT_PLOT_CONFIG_PATH])

    def test_config_without_csv_opens_and_exits(self) -> None:
        runner = CliRunner()
        opened_paths: list[str] = []

        with patch("tools.plot._open_path_for_edit", side_effect=lambda p: opened_paths.append(p)):
            result = runner.invoke(plot.app, ["--config"])

        self.assertEqual(result.exit_code, 0)
        self.assertTrue(os.path.isfile(plot._DEFAULT_PLOT_CONFIG_PATH))
        self.assertEqual(opened_paths, [plot._DEFAULT_PLOT_CONFIG_PATH])

    def test_plot_without_csv_and_without_config_fails(self) -> None:
        runner = CliRunner()
        result = runner.invoke(plot.app, [])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("FILE is required unless --config or --config-show is used.", result.output)


if __name__ == "__main__":
    unittest.main()
