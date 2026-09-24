from __future__ import annotations

import os
from collections import Counter
from datetime import date, timedelta
from typing import Any

# Same-day expiry is brutal for option buyers, so the chain can be rolled forward.
MIN_DAYS_TO_EXPIRY = int(os.getenv("MIN_DAYS_TO_EXPIRY", "1"))
EXPIRY_DEPTH = int(os.getenv("EXPIRY_DEPTH", "2"))


def tradable_expiries(expiries, today: date | None = None) -> list[str]:
    cutoff = ((today or date.today()) + timedelta(days=MIN_DAYS_TO_EXPIRY)).isoformat()
    usable = sorted(e for e in expiries if e and e >= cutoff)
    return usable or sorted(e for e in expiries if e and e >= (today or date.today()).isoformat())


def expiries_for(metadata: dict[str, dict[str, Any]], underlying: str) -> list[str]:
    expiries = {
        meta.get("expiry")
        for meta in metadata.values()
        if meta.get("instrumentType") == "OPT" and meta.get("underlying") == underlying
    }
    return tradable_expiries(expiries)[:EXPIRY_DEPTH]


def nearest_expiry(metadata: dict[str, dict[str, Any]], underlying: str) -> str | None:
    expiries = {
        meta.get("expiry")
        for meta in metadata.values()
        if meta.get("instrumentType") == "OPT" and meta.get("underlying") == underlying
    }
    usable = tradable_expiries(expiries)
    return usable[0] if usable else None


def build_chain(
    metadata: dict[str, dict[str, Any]],
    latest: dict[str, dict[str, Any]],
    underlying: str,
    expiry: str,
) -> dict[float, dict[str, Any]]:
    strikes: dict[float, dict[str, Any]] = {}
    for token, meta in metadata.items():
        if meta.get("instrumentType") != "OPT" or meta.get("underlying") != underlying:
            continue
        if meta.get("expiry") != expiry:
            continue
        strike, kind = meta.get("strike"), meta.get("optionType")
        if strike is None or kind not in ("CE", "PE"):
            continue
        quote = latest.get(token)
        if not quote or not quote.get("ltp"):
            continue
        strikes.setdefault(float(strike), {})[kind] = {
            "token": token,
            "symbol": meta.get("symbol"),
            "ltp": float(quote["ltp"]),
            "oi": quote.get("oi"),
            "oiChange": quote.get("oiChange"),
            "lotSize": meta.get("lotSize"),
        }
    return strikes


def strike_step(strikes: dict[float, Any]) -> float | None:
    values = sorted(strikes)
    if len(values) < 3:
        return None
    gaps = Counter(round(b - a, 2) for a, b in zip(values, values[1:]) if b > a)
    return gaps.most_common(1)[0][0] if gaps else None


def atm_strike(strikes: dict[float, Any], spot: float) -> float | None:
    return min(strikes, key=lambda value: abs(value - spot)) if strikes else None


def oi_profile(strikes: dict[float, dict[str, Any]], spot: float) -> dict[str, Any] | None:
    """Call/put OI walls act as resistance/support; falling wall OI signals unwinding."""
    call_oi = {k: v["CE"]["oi"] for k, v in strikes.items() if v.get("CE") and v["CE"].get("oi")}
    put_oi = {k: v["PE"]["oi"] for k, v in strikes.items() if v.get("PE") and v["PE"].get("oi")}
    if not call_oi or not put_oi:
        return None

    total_call = sum(call_oi.values())
    total_put = sum(put_oi.values())
    call_wall = max(call_oi, key=call_oi.get)
    put_wall = max(put_oi, key=put_oi.get)
    call_change = strikes[call_wall]["CE"].get("oiChange")
    put_change = strikes[put_wall]["PE"].get("oiChange")

    score = 0
    reasons: list[str] = []
    pcr = round(total_put / total_call, 2) if total_call else None
    if pcr is not None:
        if pcr >= 1.2:
            score += 1
            reasons.append(f"PCR {pcr} shows put writing support")
        elif pcr <= 0.8:
            score -= 1
            reasons.append(f"PCR {pcr} shows call writing pressure")
        else:
            reasons.append(f"PCR {pcr} is balanced")
    if call_change is not None and call_change < 0:
        score += 1
        reasons.append(f"call unwinding at {call_wall} resistance")
    if put_change is not None and put_change < 0:
        score -= 1
        reasons.append(f"put unwinding at {put_wall} support")
    reasons.append(f"call wall {call_wall}, put wall {put_wall}, spot {round(spot, 2)}")

    return {
        "pcr": pcr,
        "callWall": call_wall,
        "putWall": put_wall,
        "callWallOi": call_oi[call_wall],
        "putWallOi": put_oi[put_wall],
        "callWallOiChange": call_change,
        "putWallOiChange": put_change,
        "totalCallOi": total_call,
        "totalPutOi": total_put,
        "score": score,
        "reasons": reasons,
    }
