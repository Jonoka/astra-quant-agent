"""Pure parsing of OKX Rubik taker rows: [timestamp, sellVol, buyVol]."""
from __future__ import annotations

import math
from typing import Any, Optional, Tuple


def parse_taker_row(row: Any) -> Optional[Tuple[float, float]]:
    """Return (buy, sell); malformed or unavailable volume is not zero.

    The timestamp is metadata, not used in volume arithmetic. Extra columns
    are permitted. Both volume columns must be finite, nonnegative numbers;
    JSON booleans/null and empty strings are not volume observations.
    """
    if not isinstance(row, (list, tuple)) or len(row) < 3:
        return None
    volumes = []
    for value in (row[2], row[1]):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            number = float(value)
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(number) or number < 0:
            return None
        volumes.append(number)
    return volumes[0], volumes[1]


def latest_taker_volumes(rows: Any) -> Optional[Tuple[float, float]]:
    """Parse only the latest row; never hide an invalid row with older data."""
    if not isinstance(rows, (list, tuple)) or not rows:
        return None
    return parse_taker_row(rows[0])
