from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY_CLOSE_HOUR = 15
EXPIRY_CLOSE_MINUTE = 30


def ist_now() -> datetime:
    return datetime.now(IST)


def years_to_expiry(expiry: str, now: datetime | None = None) -> float | None:
    try:
        expiry_date = date.fromisoformat(expiry)
    except (TypeError, ValueError):
        return None
    now = now or ist_now()
    close = datetime(
        expiry_date.year,
        expiry_date.month,
        expiry_date.day,
        EXPIRY_CLOSE_HOUR,
        EXPIRY_CLOSE_MINUTE,
        tzinfo=IST,
    )
    seconds = (close - now).total_seconds()
    return seconds / (365 * 24 * 3600) if seconds > 0 else None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1_d2(spot: float, strike: float, t: float, rate: float, sigma: float):
    if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        return None
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return d1, d1 - sigma * math.sqrt(t)


def option_price(spot: float, strike: float, t: float, rate: float, sigma: float, kind: str) -> float:
    values = _d1_d2(spot, strike, t, rate, sigma)
    if values is None:
        return max(0.0, spot - strike if kind == "CE" else strike - spot)
    d1, d2 = values
    if kind == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t) * _norm_cdf(d2)
    return strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(
    market_price: float, spot: float, strike: float, t: float, rate: float, kind: str
) -> float | None:
    """Bisection inversion; returns None when the quote has no solvable IV."""
    if not market_price or market_price <= 0 or t is None or t <= 0:
        return None
    intrinsic = max(0.0, spot - strike if kind == "CE" else strike - spot)
    if market_price < intrinsic:
        return None
    low, high = 0.001, 5.0
    for _ in range(80):
        mid = 0.5 * (low + high)
        if option_price(spot, strike, t, rate, mid, kind) > market_price:
            high = mid
        else:
            low = mid
    iv = 0.5 * (low + high)
    return round(iv, 4) if 0.005 < iv < 4.9 else None


def delta(spot: float, strike: float, t: float, rate: float, sigma: float, kind: str) -> float | None:
    values = _d1_d2(spot, strike, t, rate, sigma)
    if values is None:
        return None
    d1 = values[0]
    return round(_norm_cdf(d1) if kind == "CE" else _norm_cdf(d1) - 1.0, 4)


def theta_per_day(spot: float, strike: float, t: float, rate: float, sigma: float, kind: str) -> float | None:
    values = _d1_d2(spot, strike, t, rate, sigma)
    if values is None:
        return None
    d1, d2 = values
    decay = -(spot * _norm_pdf(d1) * sigma) / (2 * math.sqrt(t))
    if kind == "CE":
        value = decay - rate * strike * math.exp(-rate * t) * _norm_cdf(d2)
    else:
        value = decay + rate * strike * math.exp(-rate * t) * _norm_cdf(-d2)
    return round(value / 365.0, 2)
