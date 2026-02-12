from __future__ import annotations

import unittest

try:
    import typer
    from tools import cheque
except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
    typer = None  # type: ignore[assignment]
    cheque = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(cheque is None, f"Missing dependency: {_IMPORT_ERROR}")
class ChequeAmountTests(unittest.TestCase):
    def test_parse_amount_integer(self) -> None:
        self.assertEqual(cheque._parse_amount("123"), (123, 0))

    def test_parse_amount_cents(self) -> None:
        self.assertEqual(cheque._parse_amount("123.45"), (123, 45))

    def test_parse_amount_commas(self) -> None:
        self.assertEqual(cheque._parse_amount("1,234.50"), (1234, 50))

    def test_parse_amount_rejects_more_than_two_decimals(self) -> None:
        with self.assertRaises(typer.BadParameter):
            cheque._parse_amount("1.234")

    def test_parse_amount_rejects_negative(self) -> None:
        with self.assertRaises(typer.BadParameter):
            cheque._parse_amount("-1")

    def test_zh_cents_wording(self) -> None:
        self.assertEqual(cheque._format_zh_amount(0, 5), "零元零伍分")
        self.assertEqual(cheque._format_zh_amount(123, 40), "壹佰貳拾叁元肆角")

    def test_en_cents_wording(self) -> None:
        self.assertEqual(
            cheque._format_en_amount(123, 45),
            "One hundred and twenty-three and forty-five cents",
        )
        self.assertEqual(cheque._format_en_amount(1, 1), "One and one cent")


if __name__ == "__main__":
    unittest.main()
