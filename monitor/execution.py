"""Only fresh, direct JD quotes may execute simulated orders."""
from __future__ import annotations

import math
from datetime import datetime

from .store import utc_now
from .trading_hours import trading_session

EXECUTION_NAME = "京东积存金（接口通用报价）"
MAX_QUOTE_AGE_SECONDS = 120


def execution_quote(snap, max_age_seconds=MAX_QUOTE_AGE_SECONDS):
    if not trading_session(utc_now())["is_open"]:
        return None
    quote = snap.get("execution_quote")
    if not isinstance(quote, dict) or quote.get("source") != "jd_latest_price_api":
        return None
    try:
        price = float(quote["price"])
        quoted = datetime.fromisoformat(quote["quote_at"])
        fetched = datetime.fromisoformat(quote["fetched_at"])
        ages = [(utc_now() - at).total_seconds() for at in (quoted, fetched)]
        if not math.isfinite(price) or price <= 0 or any(age < -30 or age > max_age_seconds for age in ages):
            return None
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    return dict(quote, price=price)
