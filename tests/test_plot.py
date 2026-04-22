from __future__ import annotations

import unittest

try:
    import typer
    from tools import plot
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    typer = None  # type: ignore[assignment]
    plot = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(plot is None, f"Missing dependency: {_IMPORT_ERROR}")
class PlotToolTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
