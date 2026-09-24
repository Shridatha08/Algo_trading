from __future__ import annotations

from typing import Any

import pandas as pd


def ema(candles: pd.DataFrame, span: int) -> float | None:
    if candles.empty or len(candles) < span:
        return None
    value = candles["close"].astype(float).ewm(span=span, adjust=False).mean().iloc[-1]
    return round(float(value), 2)


def session_vwap(candles: pd.DataFrame) -> float | None:
    """VWAP must re-anchor each session, so only the latest trading day is used."""
    if candles.empty:
        return None
    timestamps = pd.to_datetime(candles["timestamp"], utc=True)
    session = candles[timestamps.dt.date == timestamps.max().date()]
    volume = session["volume"].astype(float)
    if volume.sum() <= 0:
        return None
    typical = (session["high"].astype(float) + session["low"].astype(float) + session["close"].astype(float)) / 3
    return round(float((typical * volume).sum() / volume.sum()), 2)


def average_true_range(candles: pd.DataFrame, period: int = 14) -> float | None:
    if candles.empty or len(candles) < period + 1:
        return None
    high = candles["high"].astype(float)
    low = candles["low"].astype(float)
    close = candles["close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    return round(float(true_range.rolling(period).mean().iloc[-1]), 2)


def swing_levels(candles: pd.DataFrame, lookback: int = 20) -> dict[str, float | None]:
    if candles.empty:
        return {"high": None, "low": None}
    window = candles.tail(lookback)
    return {
        "high": round(float(window["high"].astype(float).max()), 2),
        "low": round(float(window["low"].astype(float).min()), 2),
    }
