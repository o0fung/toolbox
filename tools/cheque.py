import typer


# User can access help message with shortcut -h
app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})


@app.callback()
def cheque(
    value: int = typer.Argument(..., help="Amount in whole dollars (integer). Outputs HK cheque wording in Chinese and English."),
):
    """CLI: render a whole-dollar amount as HK cheque wording in Chinese & English.

    Contract:
    - Input: non-negative integer (Hong Kong dollars, no cents).
    - Output: two lines printed to stdout.
        中文：港幣<大寫金額>元正
        English: Hong Kong Dollars <words> only
    - Error: negative input -> Typer BadParameter.
    """
    if value < 0:
        raise typer.BadParameter("Amount must be a non-negative integer")

    # 0 is a valid amount on some forms; render explicitly
    zh = _to_trad_chinese_upper(value)
    en = _to_english_hk(value)

    print(f"中文：港幣{zh}元正")
    print(f"English: Hong Kong Dollars {en} only")


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
if __name__ == '__main__':
    app()
