from __future__ import annotations

from decimal import Decimal, InvalidOperation

import typer

try:
    from ._cli_common import new_typer_app
except ImportError:  # pragma: no cover - direct script execution fallback
    from tools._cli_common import new_typer_app


app = new_typer_app()

# English helper currently supports up to trillion group (< 10^15)
_MAX_DOLLARS = 10**15 - 1
_CENT_SCALE = Decimal("0.01")


@app.callback()
def cheque(
    value: str = typer.Argument(
        ...,
        metavar="AMOUNT",
        help="HKD amount (e.g. 123, 123.4, 123.45). Supports cents up to 2 decimal places.",
    ),
):
    """Render HK cheque wording in Traditional Chinese and English.

    Input:
    - Non-negative numeric amount in HKD with up to 2 decimal places.

    Output:
    - Chinese line: 中文：港幣<大寫金額>
    - English line: English: Hong Kong Dollars <words> only
    """
    dollars, cents = _parse_amount(value)
    _ensure_supported_range(dollars)

    zh = _format_zh_amount(dollars, cents)
    en = _format_en_amount(dollars, cents)

    typer.echo(f"中文：港幣{zh}")
    typer.echo(f"English: Hong Kong Dollars {en} only")


def _parse_amount(raw: str) -> tuple[int, int]:
    """Parse string amount into (dollars, cents) with 2dp validation."""
    text = raw.strip().replace(",", "")
    if not text:
        raise typer.BadParameter("Amount is required")

    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise typer.BadParameter("Amount must be a non-negative number, e.g. 123 or 123.45") from exc

    if not amount.is_finite():
        raise typer.BadParameter("Amount must be a finite number")
    if amount < 0:
        raise typer.BadParameter("Amount must be non-negative")

    quantized = amount.quantize(_CENT_SCALE)
    if amount != quantized:
        raise typer.BadParameter("Amount can have at most 2 decimal places")

    minor_units = int((quantized * 100).to_integral_value())
    dollars, cents = divmod(minor_units, 100)
    return dollars, cents


def _ensure_supported_range(dollars: int) -> None:
    """Validate scale limits supported by wording helpers."""
    if dollars > _MAX_DOLLARS:
        raise typer.BadParameter(
            f"Amount too large. Maximum supported dollars is {_MAX_DOLLARS}."
        )


def _format_zh_amount(dollars: int, cents: int) -> str:
    """Format full amount in Chinese cheque style."""
    dollars_text = _to_trad_chinese_upper(dollars)
    if cents == 0:
        return f"{dollars_text}元正"
    return f"{dollars_text}元{_to_zh_cents(cents)}"


def _to_zh_cents(cents: int) -> str:
    """Convert 0..99 cents to Chinese 角/分 wording."""
    assert 0 <= cents < 100
    if cents == 0:
        return "正"

    jiao, fen = divmod(cents, 10)
    parts: list[str] = []

    if jiao:
        parts.append(_DIGITS_UPPER[jiao] + "角")
    elif fen:
        parts.append("零")

    if fen:
        parts.append(_DIGITS_UPPER[fen] + "分")

    return "".join(parts)


def _format_en_amount(dollars: int, cents: int) -> str:
    """Format full amount in English cheque style."""
    dollars_text = _to_english_hk(dollars)
    if cents == 0:
        return dollars_text

    cents_text = _under_100_to_words(cents)
    cents_unit = "cent" if cents == 1 else "cents"
    return f"{dollars_text} and {cents_text} {cents_unit}"


# --- Formatting helpers ----------------------------------------------------

# Financial uppercase numerals commonly used on HK cheques
_DIGITS_UPPER = {
    0: "零",
    1: "壹",
    2: "貳",
    3: "叁",
    4: "肆",
    5: "伍",
    6: "陸",
    7: "柒",
    8: "捌",
    9: "玖",
}

# Units inside a 4-digit group (thousand/hundred/ten/one)
_UNIT_WITHIN_GROUP = ["仟", "佰", "拾", ""]  # thousands, hundreds, tens, ones
_GROUP_UNITS = ["", "萬", "億", "兆"]


def _convert_group_zh(n: int) -> str:
    """Convert 0..9999 to HK financial uppercase WITHOUT group unit.

    Rules in this 4-digit scope:
    - Use explicit tens: 10 -> 壹拾 (never just 拾).
    - Collapse consecutive internal zeros into a single 零 between non-zero units.
    - No leading 零 at the beginning of the group output.
    """
    assert 0 <= n <= 9999
    if n == 0:
        return ""

    digits = [
        (n // 1000) % 10,
        (n // 100) % 10,
        (n // 10) % 10,
        n % 10,
    ]

    parts: list[str] = []
    zero_pending = False
    for i, d in enumerate(digits):
        if d == 0:
            zero_pending = True
            continue
        if zero_pending:
            # Insert 零 only if something has been emitted in this group already
            if parts:
                parts.append("零")
            zero_pending = False
        parts.append(_DIGITS_UPPER[d] + _UNIT_WITHIN_GROUP[i])
    return "".join(parts)


def _to_trad_chinese_upper(n: int) -> str:
    """Convert a non-negative integer to HK cheque wording in Traditional Chinese.

    Grouping uses base-10000 units: 萬(10^4), 億(10^8), 兆(10^12).
    Example: 120034 -> "壹拾貳萬零叁拾肆"
    """
    if n == 0:
        return "零"

    groups: list[int] = []  # base-10000 groups, lowest first
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    if len(groups) > len(_GROUP_UNITS):
        raise ValueError("Chinese wording scale exceeded")

    parts: list[str] = []
    zero_pending = False  # crossed one/more empty 10^4 groups since last non-zero
    for idx in range(len(groups) - 1, -1, -1):  # traverse high -> low
        g = groups[idx]
        if g == 0:
            if parts:  # we have higher non-zero content already
                zero_pending = True
            continue
        seg = _convert_group_zh(g)
        # Insert a single 零 when:
        # 1) we crossed empty groups previously (zero_pending), or
        # 2) linking a higher group to a lower non-zero group whose value < 1000
        #    (meaning leading zeros within this 4-digit block), e.g., 1,000,001 -> 壹佰萬零壹
        if parts and (zero_pending or g < 1000):
            parts.append("零")
            zero_pending = False
        parts.append(seg + _GROUP_UNITS[idx])

    return "".join(parts)


# English words for 0..19
_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
# English tens (20..90)
_TENS = [
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]
_SCALE_EN = ["", "thousand", "million", "billion", "trillion"]  # extend if needed


def _under_100_to_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, r = divmod(n, 10)
    return _TENS[t] + ("-" + _ONES[r] if r else "")


def _under_1000_to_words(n: int) -> str:
    assert 0 <= n < 1000
    h, r = divmod(n, 100)
    parts: list[str] = []
    if h:
        parts.append(_ONES[h] + " hundred")
        if r:
            parts.append("and " + _under_100_to_words(r))
    else:
        if r:
            parts.append(_under_100_to_words(r))
    return " ".join(parts)


def _to_english_hk(n: int) -> str:
    """Convert a non-negative integer to English words (HK/British style).

    Rules:
    - Use "and" inside hundreds (e.g., one hundred and two).
    - Hyphenate 21–99 (e.g., twenty-one).
    - If there are higher groups and the last (lowest) group is < 100,
        insert "and" before the final segment (e.g., one thousand and ten).
    """
    if n == 0:
        return "Zero"  # Capitalize first letter for cheque aesthetics

    groups: list[int] = []
    while n > 0:
        groups.append(n % 1000)
        n //= 1000
    if len(groups) > len(_SCALE_EN):
        raise ValueError("English wording scale exceeded")

    words_parts: list[str] = []
    numeric_parts: list[int] = []
    for idx, g in enumerate(groups):
        if g == 0:
            continue
        seg = _under_1000_to_words(g)
        scale = _SCALE_EN[idx]
        words_parts.append((seg + (" " + scale if scale else "")).strip())
        numeric_parts.append(g)

    # Combine from highest scale to lowest
    words_parts = list(reversed(words_parts))
    numeric_parts = list(reversed(numeric_parts))

    if len(words_parts) > 1 and numeric_parts[-1] < 100:
        # Insert an "and" before the final segment
        words = " ".join(words_parts[:-1] + ["and " + words_parts[-1]])
    else:
        words = " ".join(words_parts)

    # Capitalise first letter only (common on cheque lines)
    return words[:1].upper() + words[1:]


# Entry point for running the script directly
if __name__ == "__main__":
    app()
