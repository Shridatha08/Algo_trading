from __future__ import annotations

import os
from typing import Any

import pandas as pd

from .indicators import average_true_range, ema, swing_levels

EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "15"))
TREND_FAST = int(os.getenv("TREND_EMA_FAST", "20"))
TREND_SLOW = int(os.getenv("TREND_EMA_SLOW", "50"))
SUPERTREND_PERIOD = int(os.getenv("SUPERTREND_PERIOD", "10"))
SUPERTREND_MULTIPLIER = float(os.getenv("SUPERTREND_MULTIPLIER", "3"))
ORB_CANDLES = int(os.getenv("ORB_CANDLES", "3"))
ORB_VALID_UNTIL = os.getenv("ORB_VALID_UNTIL", "14:00")
VWAP_STRETCH_ATR = float(os.getenv("VWAP_STRETCH_ATR", "1.5"))
MIN_TREND_SEPARATION = float(os.getenv("MIN_TREND_SEPARATION", "0.15"))
FRESH_BARS = int(os.getenv("SIGNAL_FRESH_BARS", "3"))
STRATEGY_PRIORITY = os.getenv("STRATEGY_PRIORITY", "orb,ema,vwap,trend").split(",")
STRATEGY_NAMES = {
    "orb": "ORB + Supertrend",
    "ema": "9/15 EMA momentum scalp",
    "vwap": "VWAP mean-reversion bounce",
    "trend": "EMA 20/50 trend + VWAP + OI",
}


def session_slice(candles: pd.DataFrame) -> pd.DataFrame:
    if candles.empty:
        return candles
    stamps = pd.to_datetime(candles["timestamp"], utc=True)
    return candles[stamps.dt.date == stamps.max().date()]


def supertrend(candles: pd.DataFrame) -> dict[str, Any] | None:
    """Classic ATR band Supertrend; returns the trailing line and its direction."""
    if len(candles) < SUPERTREND_PERIOD + 2:
        return None
    high = candles["high"].astype(float).reset_index(drop=True)
    low = candles["low"].astype(float).reset_index(drop=True)
    close = candles["close"].astype(float).reset_index(drop=True)

    previous = close.shift(1)
    true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    atr = true_range.rolling(SUPERTREND_PERIOD).mean()
    mid = (high + low) / 2
    upper_band = mid + SUPERTREND_MULTIPLIER * atr
    lower_band = mid - SUPERTREND_MULTIPLIER * atr

    upper: list[float] = []
    lower: list[float] = []
    direction: list[int] = []
    for index in range(len(close)):
        if index == 0 or pd.isna(atr.iloc[index]):
            upper.append(float(upper_band.iloc[index]) if not pd.isna(upper_band.iloc[index]) else float(high.iloc[index]))
            lower.append(float(lower_band.iloc[index]) if not pd.isna(lower_band.iloc[index]) else float(low.iloc[index]))
            direction.append(1)
            continue
        current_upper = float(upper_band.iloc[index])
        current_lower = float(lower_band.iloc[index])
        # Bands only tighten while the trend holds, which is what makes the line trail.
        final_upper = current_upper if current_upper < upper[-1] or close.iloc[index - 1] > upper[-1] else upper[-1]
        final_lower = current_lower if current_lower > lower[-1] or close.iloc[index - 1] < lower[-1] else lower[-1]
        trend = direction[-1]
        if close.iloc[index] > final_upper:
            trend = 1
        elif close.iloc[index] < final_lower:
            trend = -1
        upper.append(final_upper)
        lower.append(final_lower)
        direction.append(trend)

    line = lower[-1] if direction[-1] == 1 else upper[-1]
    return {"direction": direction[-1], "line": round(float(line), 2)}


def opening_range(candles: pd.DataFrame) -> dict[str, float] | None:
    session = session_slice(candles)
    if len(session) <= ORB_CANDLES:
        return None
    opening = session.head(ORB_CANDLES)
    return {
        "high": round(float(opening["high"].astype(float).max()), 2),
        "low": round(float(opening["low"].astype(float).min()), 2),
    }


def _ema_series(candles: pd.DataFrame, span: int) -> pd.Series:
    return candles["close"].astype(float).ewm(span=span, adjust=False).mean()


def _recently(series: pd.Series, bullish: bool) -> bool:
    """True when the series changed side within the freshness window, so entries are not chased."""
    window = series.iloc[-(FRESH_BARS + 1) : -1]
    if window.empty:
        return False
    return bool((window <= 0).any()) if bullish else bool((window >= 0).any())


def ema_momentum_scalp(candles: pd.DataFrame) -> dict[str, Any] | None:
    """9/15 EMA momentum: trade only with separation and a candle pushing the trend."""
    fast = ema(candles, EMA_FAST)
    slow = ema(candles, EMA_SLOW)
    atr = average_true_range(candles)
    if fast is None or slow is None or not atr or len(candles) < EMA_SLOW + 2:
        return None
    if abs(fast - slow) < MIN_TREND_SEPARATION * atr:
        return None

    close = float(candles["close"].astype(float).iloc[-1])
    previous_close = float(candles["close"].astype(float).iloc[-2])
    recent_low = round(float(candles["low"].astype(float).tail(5).min()), 2)
    recent_high = round(float(candles["high"].astype(float).tail(5).max()), 2)
    separation = _ema_series(candles, EMA_FAST) - _ema_series(candles, EMA_SLOW)
    reclaim = candles["close"].astype(float) - _ema_series(candles, EMA_FAST)

    if fast > slow and close > fast and close > previous_close:
        if not (_recently(separation, True) or _recently(reclaim, True)):
            return None
        return {
            "name": "9/15 EMA momentum scalp",
            "direction": "CE",
            "stopSpot": min(slow, recent_low),
            "reasons": [f"EMA{EMA_FAST} {fast} above EMA{EMA_SLOW} {slow} with price {close} pushing higher"],
        }
    if fast < slow and close < fast and close < previous_close:
        if not (_recently(separation, False) or _recently(reclaim, False)):
            return None
        return {
            "name": "9/15 EMA momentum scalp",
            "direction": "PE",
            "stopSpot": max(slow, recent_high),
            "reasons": [f"EMA{EMA_FAST} {fast} below EMA{EMA_SLOW} {slow} with price {close} pushing lower"],
        }
    return None


def orb_supertrend(candles: pd.DataFrame, now) -> dict[str, Any] | None:
    """Opening range breakout, only taken when Supertrend agrees with the break."""
    if now.strftime("%H:%M") > ORB_VALID_UNTIL:
        return None
    levels = opening_range(candles)
    trend = supertrend(candles)
    if levels is None or trend is None:
        return None

    close = float(candles["close"].astype(float).iloc[-1])
    closes = candles["close"].astype(float)
    recent = closes.iloc[-(FRESH_BARS + 1) : -1]
    if close > levels["high"] and trend["direction"] == 1:
        # Only the first candles after the break qualify; later ones are chasing.
        if recent.empty or not bool((recent <= levels["high"]).any()):
            return None
        return {
            "name": "ORB + Supertrend",
            "direction": "CE",
            "stopSpot": max(levels["low"], trend["line"]),
            "reasons": [
                f"broke opening range high {levels['high']} with Supertrend bullish at {trend['line']}"
            ],
        }
    if close < levels["low"] and trend["direction"] == -1:
        if recent.empty or not bool((recent >= levels["low"]).any()):
            return None
        return {
            "name": "ORB + Supertrend",
            "direction": "PE",
            "stopSpot": min(levels["high"], trend["line"]),
            "reasons": [
                f"broke opening range low {levels['low']} with Supertrend bearish at {trend['line']}"
            ],
        }
    return None


def vwap_bounce(candles: pd.DataFrame, vwap: float | None) -> dict[str, Any] | None:
    """Mean reversion: price stretched from VWAP, then a reversal candle back toward it."""
    if vwap is None or len(candles) < 3:
        return None
    atr = average_true_range(candles)
    if not atr:
        return None

    last = candles.iloc[-1]
    previous_close = float(candles["close"].astype(float).iloc[-2])
    close, open_price = float(last["close"]), float(last["open"])
    low, high = float(last["low"]), float(last["high"])
    stretch = round((vwap - close) / atr, 2)

    if stretch >= VWAP_STRETCH_ATR and close > open_price and close > previous_close:
        return {
            "name": "VWAP mean-reversion bounce",
            "direction": "CE",
            "stopSpot": low,
            "reasons": [f"price {stretch} ATR below VWAP {vwap} with a reversal candle closing up"],
        }
    if -stretch >= VWAP_STRETCH_ATR and close < open_price and close < previous_close:
        return {
            "name": "VWAP mean-reversion bounce",
            "direction": "PE",
            "stopSpot": high,
            "reasons": [f"price {abs(stretch)} ATR above VWAP {vwap} with a reversal candle closing down"],
        }
    return None


def trend_vwap_oi(candles: pd.DataFrame, vwap: float | None, oi_score: int = 0) -> dict[str, Any] | None:
    """Slower EMA20/50 trend with VWAP side and open interest agreeing."""
    fast = ema(candles, TREND_FAST)
    slow = ema(candles, TREND_SLOW)
    if fast is None or slow is None:
        return None

    close = float(candles["close"].astype(float).iloc[-1])
    score = 0
    reasons: list[str] = []
    if close > fast > slow:
        score += 1
        reasons.append(f"price {close} above EMA{TREND_FAST} {fast} above EMA{TREND_SLOW} {slow}")
    elif close < fast < slow:
        score -= 1
        reasons.append(f"price {close} below EMA{TREND_FAST} {fast} below EMA{TREND_SLOW} {slow}")
    else:
        return None

    if vwap is not None:
        if close > vwap:
            score += 1
            reasons.append(f"holding above VWAP {vwap}")
        elif close < vwap:
            score -= 1
            reasons.append(f"trading below VWAP {vwap}")

    swings = swing_levels(candles)
    reclaim = candles["close"].astype(float) - _ema_series(candles, TREND_FAST)
    if score > 0 and oi_score >= 0:
        if not _recently(reclaim, True):
            return None
        return {
            "name": STRATEGY_NAMES["trend"],
            "direction": "CE",
            "stopSpot": swings["low"] or slow,
            "reasons": reasons + [f"open interest score {oi_score} not opposing"],
        }
    if score < 0 and oi_score <= 0:
        if not _recently(reclaim, False):
            return None
        return {
            "name": STRATEGY_NAMES["trend"],
            "direction": "PE",
            "stopSpot": swings["high"] or slow,
            "reasons": reasons + [f"open interest score {oi_score} not opposing"],
        }
    return None


def higher_timeframe_bias(candles: pd.DataFrame, minutes: int = 15) -> str | None:
    """Resample to a slower timeframe so trades are not taken against the bigger trend."""
    if candles.empty:
        return None
    frame = candles.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("timestamp").sort_index()
    aggregated = (
        frame.resample(f"{minutes}min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    if len(aggregated) < 22:
        return None
    close = aggregated["close"].astype(float)
    fast = close.ewm(span=9, adjust=False).mean().iloc[-1]
    slow = close.ewm(span=21, adjust=False).mean().iloc[-1]
    last = close.iloc[-1]
    if last > fast > slow:
        return "up"
    if last < fast < slow:
        return "down"
    return "flat"


def consensus(fired: list[dict[str, Any]]) -> dict[str, Any]:
    """Group signals by side; opposing signals are a conflict, not a trade."""
    calls = [s for s in fired if s["direction"] == "CE"]
    puts = [s for s in fired if s["direction"] == "PE"]
    if calls and puts:
        return {
            "direction": None,
            "conflict": True,
            "agree": [],
            "callNames": [s["name"] for s in calls],
            "putNames": [s["name"] for s in puts],
        }
    agree = calls or puts
    return {
        "direction": agree[0]["direction"] if agree else None,
        "conflict": False,
        "agree": [s["name"] for s in agree],
        "callNames": [s["name"] for s in calls],
        "putNames": [s["name"] for s in puts],
    }


def scan(candles: pd.DataFrame, vwap: float | None, now, oi_score: int = 0) -> dict[str, Any]:
    """Run every enabled strategy so the caller can compare them, not just take the first."""
    builders = {
        "orb": lambda: orb_supertrend(candles, now),
        "ema": lambda: ema_momentum_scalp(candles),
        "vwap": lambda: vwap_bounce(candles, vwap),
        "trend": lambda: trend_vwap_oi(candles, vwap, oi_score),
    }
    fired: list[dict[str, Any]] = []
    idle: list[str] = []
    for key in STRATEGY_PRIORITY:
        key = key.strip().lower()
        builder = builders.get(key)
        if builder is None:
            continue
        signal = builder()
        if signal:
            fired.append(signal)
        else:
            idle.append(STRATEGY_NAMES[key])
    return {"fired": fired, "idle": idle}
