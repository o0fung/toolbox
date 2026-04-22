from __future__ import annotations

import unittest
import warnings
from typing import List, Sequence, Tuple

try:
    from typer.main import get_command
    from tools import cheque, clock, pdf, plot, youtube
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    get_command = None  # type: ignore[assignment]
    cheque = None  # type: ignore[assignment]
    clock = None  # type: ignore[assignment]
    pdf = None  # type: ignore[assignment]
    plot = None  # type: ignore[assignment]
    youtube = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _group_context(command, args: Sequence[str]):
    return command.make_context(command.name or "tool", list(args), resilient_parsing=True)


def _split_group_tokens(ctx) -> List[str]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        protected = list(getattr(ctx, "protected_args", []) or [])
    return protected + list(ctx.args)


@unittest.skipIf(get_command is None, f"Missing dependency: {_IMPORT_ERROR}")
class CallbackOptionOrderingPhaseOneTests(unittest.TestCase):
    def test_plot_accepts_option_after_csv_argument(self) -> None:
        command = get_command(plot.app)
        before_ctx = _group_context(command, ["--xlim", "1,2", "/tmp/data.csv"])
        after_ctx = _group_context(command, ["/tmp/data.csv", "--xlim", "1,2"])

        self.assertEqual(before_ctx.params["csv_path"], "/tmp/data.csv")
        self.assertEqual(before_ctx.params["xlim"], "1,2")
        self.assertEqual(before_ctx.args, [])

        self.assertEqual(after_ctx.params["csv_path"], "/tmp/data.csv")
        self.assertEqual(after_ctx.params["xlim"], "1,2")
        self.assertEqual(after_ctx.args, [])

    def test_pdf_accepts_option_after_input_argument(self) -> None:
        command = get_command(pdf.app)
        before_ctx = _group_context(command, ["-q", "screen", "/tmp/in.pdf"])
        after_ctx = _group_context(command, ["/tmp/in.pdf", "-q", "screen"])

        self.assertEqual(before_ctx.params["input_pdf"], "/tmp/in.pdf")
        self.assertEqual(before_ctx.params["quality"], "screen")
        self.assertEqual(before_ctx.args, [])

        self.assertEqual(after_ctx.params["input_pdf"], "/tmp/in.pdf")
        self.assertEqual(after_ctx.params["quality"], "screen")
        self.assertEqual(after_ctx.args, [])

    def test_youtube_accepts_option_after_url_argument(self) -> None:
        command = get_command(youtube.app)
        before_ctx = _group_context(command, ["-a", "https://example.com/watch?v=abc"])
        after_ctx = _group_context(command, ["https://example.com/watch?v=abc", "-a"])

        self.assertEqual(before_ctx.params["url"], "https://example.com/watch?v=abc")
        self.assertTrue(before_ctx.params["audio"])
        self.assertEqual(before_ctx.args, [])

        self.assertEqual(after_ctx.params["url"], "https://example.com/watch?v=abc")
        self.assertTrue(after_ctx.params["audio"])
        self.assertEqual(after_ctx.args, [])

    def test_cheque_positional_parsing_stays_stable(self) -> None:
        command = get_command(cheque.app)
        ctx = _group_context(command, ["123.45"])
        self.assertEqual(ctx.params["value"], "123.45")
        self.assertEqual(ctx.args, [])


@unittest.skipIf(get_command is None, f"Missing dependency: {_IMPORT_ERROR}")
class ClockOptionOrderingPhaseTwoTests(unittest.TestCase):
    def _parse_clock_subcommand(self, args: Sequence[str]) -> Tuple[object, str, object]:
        root_cmd = get_command(clock.app)
        root_ctx = _group_context(root_cmd, args)
        # Parse clock in two stages (group then subcommand) to verify callback
        # vs subcommand option ownership without invoking runtime side effects.
        tokens = _split_group_tokens(root_ctx)
        self.assertTrue(tokens, "clock parse must include a subcommand token")

        sub_name = tokens[0]
        sub_cmd = root_cmd.get_command(root_ctx, sub_name)
        self.assertIsNotNone(sub_cmd, f"unknown subcommand: {sub_name}")
        sub_ctx = sub_cmd.make_context(sub_name, tokens[1:], parent=root_ctx, resilient_parsing=True)
        return root_ctx, sub_name, sub_ctx

    def test_clock_keeps_parent_options_before_subcommand(self) -> None:
        root_ctx, sub_name, sub_ctx = self._parse_clock_subcommand(["-c", "red", "timer"])
        self.assertEqual(sub_name, "timer")
        self.assertEqual(root_ctx.params["color"], "red")
        self.assertEqual(sub_ctx.params["color"], "cyan")

    def test_clock_countdown_allows_subcommand_option_after_values(self) -> None:
        root_ctx, sub_name, sub_ctx = self._parse_clock_subcommand(["countdown", "1", "30", "-c", "green"])
        self.assertEqual(sub_name, "countdown")
        self.assertEqual(root_ctx.params["color"], "cyan")
        self.assertEqual(list(sub_ctx.params["values"]), ["1", "30"])
        self.assertEqual(sub_ctx.params["color"], "green")

    def test_clock_countdown_supports_mixed_option_placement(self) -> None:
        root_ctx, sub_name, sub_ctx = self._parse_clock_subcommand(
            ["-s", "large", "countdown", "1", "30", "-c", "yellow"]
        )
        self.assertEqual(sub_name, "countdown")
        self.assertEqual(root_ctx.params["size"], "large")
        self.assertEqual(list(sub_ctx.params["values"]), ["1", "30"])
        self.assertEqual(sub_ctx.params["color"], "yellow")


if __name__ == "__main__":
    unittest.main()
