"""Pure parsing of OKX Rubik taker rows: [timestamp, sellVol, buyVol]."""
from __future__ import annotations

import math
import re
from decimal import Context, Decimal, InvalidOperation, MAX_EMAX, MIN_EMIN, ROUND_HALF_EVEN, localcontext
from typing import Any, Optional, Sequence, Tuple

_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
DecimalVolumes = Tuple[Decimal, Decimal]


def _finite_float(value: Decimal) -> Optional[float]:
    """Keep the existing float output domain without turning underflow into zero."""
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or (value != 0 and number == 0):
        return None
    return number


def parse_taker_row_decimal(row: Any) -> Optional[DecimalVolumes]:
    """Validate original decimal values before any lossy float conversion.

    JSON floats use their shortest decimal representation; the original token
    is already unavailable. OKX string volumes and integer volumes stay exact.
    Signed mathematical zero is legal; any negative nonzero volume is not.
    """
    if not isinstance(row, (list, tuple)) or len(row) < 3:
        return None
    volumes = []
    for value in (row[2], row[1]):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            text = str(value).strip()
            if not _NUMBER.fullmatch(text):
                return None
            decimal = Decimal(text)
        except (InvalidOperation, ValueError, OverflowError):
            return None
        if not decimal.is_finite() or decimal < 0 or _finite_float(decimal) is None:
            return None
        # Zero's exponent is irrelevant; prevent extreme zero exponents from
        # inflating the exact-arithmetic context while preserving signed zero.
        volumes.append(Decimal(0).copy_sign(decimal) if decimal == 0 else decimal)
    return volumes[0], volumes[1]


def latest_taker_volumes_decimal(rows: Any) -> Optional[DecimalVolumes]:
    """Parse only the newest row, preserving decimal precision for arithmetic."""
    if not isinstance(rows, (list, tuple)) or not rows:
        return None
    return parse_taker_row_decimal(rows[0])


def parse_taker_row(row: Any) -> Optional[Tuple[float, float]]:
    """Return (buy, sell); malformed or unavailable volume is not zero.

    The timestamp is metadata, not used in volume arithmetic. Extra columns
    are permitted. Both volume columns must be finite, nonnegative numbers;
    JSON booleans/null and empty strings are not volume observations.
    This float compatibility wrapper must not be used for net arithmetic;
    the decimal helpers/latest_taker_net preserve original operand precision.
    """
    volumes = parse_taker_row_decimal(row)
    return (float(volumes[0]), float(volumes[1])) if volumes is not None else None


def latest_taker_volumes(rows: Any) -> Optional[Tuple[float, float]]:
    """Parse only the latest row; never hide an invalid row with older data."""
    if not isinstance(rows, (list, tuple)) or not rows:
        return None
    return parse_taker_row(rows[0])


def _exact_sum(values: Sequence[Decimal]) -> Decimal:
    """Align decimal exponents with enough precision for every digit and carry.

    The default Decimal precision (28) can also erase a small difference
    between large operands. Derive precision from the actual input span;
    copy_negate below avoids context rounding before this sum starts.
    """
    nonzero = [value for value in values if value != 0]
    if not nonzero:
        return Decimal(0)
    low = min(value.as_tuple().exponent for value in nonzero)
    high = max(value.adjusted() for value in nonzero)
    precision = max(28, high - low + 1 + len(str(len(nonzero))))
    with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN,
                              Emax=MAX_EMAX, Emin=MIN_EMIN)):
        return sum(nonzero, Decimal(0))


def sum_taker_net(volumes: Sequence[DecimalVolumes]) -> Optional[Decimal]:
    """Exact buy-minus-sell total; unsupported output magnitudes remain missing."""
    values = [value for buy, sell in volumes for value in (buy, sell.copy_negate())]
    total = _exact_sum(values)
    return total if _finite_float(total) is not None else None


def taker_net_float(volumes: DecimalVolumes) -> Optional[float]:
    """Convert only the final exact net, preserving existing numeric outputs."""
    total = sum_taker_net([volumes])
    return _finite_float(total) if total is not None else None


def latest_taker_net(rows: Any) -> Optional[float]:
    volumes = latest_taker_volumes_decimal(rows)
    return taker_net_float(volumes) if volumes is not None else None


def taker_ratio_float(volumes: DecimalVolumes) -> Optional[float]:
    buy, sell = volumes
    if sell == 0:
        return None
    precision = max(34, len(buy.as_tuple().digits) + len(sell.as_tuple().digits) + 20)
    with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN,
                              Emax=MAX_EMAX, Emin=MIN_EMIN)):
        return _finite_float(buy / sell)
