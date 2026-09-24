from __future__ import annotations

import os
from typing import Any

from .chain import atm_strike, oi_profile, strike_step
from .greeks import delta, implied_volatility, option_price, theta_per_day, years_to_expiry
from .indicators import average_true_range, ema
from .signals import EMA_FAST, EMA_SLOW, consensus, higher_timeframe_bias, opening_range, scan, supertrend

RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.065"))
MIN_RR = float(os.getenv("MIN_RISK_REWARD", "1.2"))
MIN_CANDLES = int(os.getenv("MIN_CANDLES", "30"))
HTF_MINUTES = int(os.getenv("HIGHER_TIMEFRAME_MINUTES", "15"))
ALLOW_CONFLICT = os.getenv("ALLOW_CONFLICT", "false").lower() in {"1", "true", "yes"}
MIN_GRADE = os.getenv("MIN_GRADE", "B").upper()
MIN_GRADE_SCORE = {"A": 5, "B": 4, "C": 3, "D": 0}.get(MIN_GRADE, 3)
TARGET_DELTA = float(os.getenv("TARGET_DELTA", "0.5"))
STOP_ATR_MIN = float(os.getenv("STOP_ATR_MIN", "1.0"))
STOP_ATR_MAX = float(os.getenv("STOP_ATR_MAX", "5.0"))
PREMIUM_SL_FRACTION = float(os.getenv("PREMIUM_SL_FRACTION", "0.35"))
MAX_PREMIUM_RISK = float(os.getenv("MAX_PREMIUM_RISK", "0.40"))
SESSION_START = os.getenv("SESSION_START", "09:20")
SESSION_END = os.getenv("SESSION_END", "15:15")
VIX_LOW = float(os.getenv("VIX_LOW", "12"))
VIX_ELEVATED = float(os.getenv("VIX_ELEVATED", "18"))
VIX_HIGH = float(os.getenv("VIX_HIGH", "25"))
STOCK_IV_LOW = float(os.getenv("STOCK_IV_LOW", "22"))
STOCK_IV_ELEVATED = float(os.getenv("STOCK_IV_ELEVATED", "35"))
STOCK_IV_HIGH = float(os.getenv("STOCK_IV_HIGH", "50"))


def _regime_from(value: float | None, low: float, elevated: float, high: float) -> str | None:
    if value is None:
        return None
    if value < low:
        return "low"
    if value < elevated:
        return "normal"
    if value < high:
        return "elevated"
    return "high"


def atm_implied_vol(chain, spot, atm, t) -> float | None:
    """Stocks have no VIX, so the regime comes from the contract's own ATM implied volatility."""
    values = []
    for kind in ("CE", "PE"):
        node = (chain.get(atm) or {}).get(kind)
        if node and node.get("ltp"):
            iv = implied_volatility(node["ltp"], spot, atm, t, RISK_FREE_RATE, kind)
            if iv:
                values.append(iv * 100)
    return round(sum(values) / len(values), 2) if values else None


def vix_regime(vix: float | None) -> str | None:
    return _regime_from(vix, VIX_LOW, VIX_ELEVATED, VIX_HIGH)


def _tick(value: float) -> float:
    return round(round(value / 0.05) * 0.05, 2)


def _entry_range(price: float) -> list[float]:
    return [_tick(price * 0.98), _tick(price * 1.02)]


def _leg(action: str, strike: float, kind: str, chain, spot, t, quantity: int = 1) -> dict[str, Any] | None:
    node = chain.get(strike, {}).get(kind)
    if not node or not node.get("ltp") or node["ltp"] <= 0:
        return None
    iv = implied_volatility(node["ltp"], spot, strike, t, RISK_FREE_RATE, kind)
    return {
        "action": action,
        "quantity": quantity,
        "token": node.get("token"),
        "symbol": node.get("symbol"),
        "strike": strike,
        "optionType": kind,
        "ltp": round(node["ltp"], 2),
        "entryRange": _entry_range(node["ltp"]),
        "iv": round(iv * 100, 2) if iv else None,
        "delta": delta(spot, strike, t, RISK_FREE_RATE, iv, kind) if iv else None,
        "thetaPerDay": theta_per_day(spot, strike, t, RISK_FREE_RATE, iv, kind) if iv else None,
        "oi": node.get("oi"),
        "lotSize": node.get("lotSize"),
    }


def _net_premium(legs: list[dict[str, Any]]) -> float:
    return round(
        sum(
            leg["ltp"] * leg.get("quantity", 1) * (1 if leg["action"] == "BUY" else -1)
            for leg in legs
        ),
        2,
    )


def _position_greeks(legs: list[dict[str, Any]]) -> dict[str, float | None]:
    def total(field: str) -> float | None:
        values = [leg[field] for leg in legs if leg.get(field) is not None]
        if len(values) != len(legs):
            return None
        return round(
            sum(
                value * leg.get("quantity", 1) * (1 if leg["action"] == "BUY" else -1)
                for value, leg in zip(values, legs)
            ),
            4,
        )

    return {"netDelta": total("delta"), "netThetaPerDay": total("thetaPerDay")}


def _in_session(now) -> bool:
    if os.getenv("IGNORE_SESSION_WINDOW", "false").lower() in {"1", "true", "yes"}:
        return True
    current = now.strftime("%H:%M")
    return SESSION_START <= current <= SESSION_END


def evaluate_underlying(
    underlying: str,
    expiry: str | None,
    spot: float | None,
    spot_candles,
    futures_vwap: float | None,
    chain: dict[float, dict[str, Any]],
    vix: float | None,
    now,
    asset_class: str = "INDEX",
) -> dict[str, Any]:
    """Return a buy-only setup from the 5-minute strategies, or an explicit no-trade."""
    blocked: list[str] = []
    if spot is None or spot <= 0:
        blocked.append("spot price unavailable")
    if expiry is None:
        blocked.append("no live expiry in option chain")
    if vix is None and asset_class == "INDEX":
        blocked.append("India VIX unavailable")
    if len(chain) < 5:
        blocked.append("option chain too thin to locate OI walls")
    if not _in_session(now):
        blocked.append(f"outside trading window {SESSION_START}-{SESSION_END} IST")
    if blocked:
        return {"instrument": underlying, "state": "no-trade", "reasons": blocked}

    time_to_expiry = years_to_expiry(expiry, now)
    if time_to_expiry is None:
        return {"instrument": underlying, "state": "no-trade", "reasons": ["expiry already closed"]}

    if len(spot_candles) < MIN_CANDLES:
        return {
            "instrument": underlying,
            "expiry": expiry,
            "state": "no-trade",
            "reasons": [
                f"insufficient candle history for the 5-minute strategies "
                f"(have {len(spot_candles)} candles, need {MIN_CANDLES})"
            ],
        }

    oi = oi_profile(chain, spot)
    if oi is None:
        return {"instrument": underlying, "state": "no-trade", "reasons": ["open interest not yet populated"]}

    step = strike_step(chain)
    atm = atm_strike(chain, spot)
    if step is None or atm is None:
        return {"instrument": underlying, "state": "no-trade", "reasons": ["strike ladder incomplete"]}

    regime = vix_regime(vix)
    if asset_class == "STOCK":
        atm_iv = atm_implied_vol(chain, spot, atm, time_to_expiry)
        regime = _regime_from(atm_iv, STOCK_IV_LOW, STOCK_IV_ELEVATED, STOCK_IV_HIGH)
        volatility = {"source": "ATM IV", "value": atm_iv}
    else:
        volatility = {"source": "India VIX", "value": vix}
    if regime is None:
        return {
            "instrument": underlying,
            "expiry": expiry,
            "state": "no-trade",
            "reasons": ["implied volatility could not be measured for this underlying"],
        }

    # Signals run on the series that carries volume; their level output is shifted back to spot.
    reference = float(spot_candles["close"].astype(float).iloc[-1])
    basis = round(reference - spot, 2)
    scanned = scan(spot_candles, futures_vwap, now, oi["score"])

    atr = average_true_range(spot_candles)
    trend = supertrend(spot_candles)
    levels = opening_range(spot_candles)

    # Indicators are measured on the futures series; shift them to spot so they line up
    # with spot prices, the option chain and the chart.
    def to_spot(value: float | None) -> float | None:
        return round(value - basis, 2) if value is not None else None

    context = {
        "spot": spot,
        "ema9": to_spot(ema(spot_candles, EMA_FAST)),
        "ema15": to_spot(ema(spot_candles, EMA_SLOW)),
        "vwap": to_spot(futures_vwap),
        "supertrend": to_spot(trend["line"]) if trend else None,
        "supertrendDirection": "up" if trend and trend["direction"] == 1 else "down" if trend else None,
        "orbHigh": to_spot(levels["high"]) if levels else None,
        "orbLow": to_spot(levels["low"]) if levels else None,
        "atr": atr,
        "basis": basis,
        "daysToExpiry": round(time_to_expiry * 365, 1),
        "vix": vix,
        "volatility": volatility,
        "vixRegime": regime,
        "pcr": oi["pcr"],
        "callWall": oi["callWall"],
        "putWall": oi["putWall"],
    }
    candidates = [{"name": name, "state": "not-triggered"} for name in scanned["idle"]]
    agreement = consensus(scanned["fired"])
    context["higherTimeframe"] = higher_timeframe_bias(spot_candles, HTF_MINUTES)
    context["confluence"] = agreement["agree"]

    if not scanned["fired"]:
        return {
            "instrument": underlying,
            "expiry": expiry,
            "view": "Neutral",
            "context": context,
            "candidates": candidates,
            "state": "no-trade",
            "reasons": ["none of the 5-minute strategies triggered on the latest candle"],
        }

    if agreement["conflict"] and not ALLOW_CONFLICT:
        return {
            "instrument": underlying,
            "expiry": expiry,
            "view": "Neutral",
            "context": context,
            "candidates": candidates
            + [
                {"name": s["name"], "direction": s["direction"], "state": "blocked", "reason": "direction conflict"}
                for s in scanned["fired"]
            ],
            "state": "no-trade",
            "reasons": [
                "strategies disagree on direction: "
                f"{', '.join(agreement['callNames'])} say CE while "
                f"{', '.join(agreement['putNames'])} say PE"
            ],
        }

    if regime == "high":
        return {
            "instrument": underlying,
            "expiry": expiry,
            "view": "Neutral",
            "context": context,
            "candidates": candidates
            + [{"name": s["name"], "state": "blocked", "reason": "IV too rich"} for s in scanned["fired"]],
            "state": "no-trade",
            "reasons": [f"{volatility['source']} {volatility['value']} is too rich to buy premium"],
        }

    # Every fired strategy is priced, then ranked by confluence grade before risk-reward.
    best: tuple[tuple[int, float], dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    for signal in scanned["fired"]:
        plan = _long_option(
            f"{signal['name']} \u2192 Long {signal['direction']}",
            signal["direction"],
            chain,
            spot,
            atm,
            step,
            oi,
            time_to_expiry,
            atr,
            round(signal["stopSpot"] - basis, 2),
        )
        if plan is None:
            candidates.append(
                {
                    "name": signal["name"],
                    "direction": signal["direction"],
                    "state": "rejected",
                    "reason": "no liquid near-the-money strike with a usable stop distance",
                }
            )
            continue

        grade = _grade(signal, agreement, context["higherTimeframe"], oi, spot, plan)
        gate = _risk_gate(plan)
        failed = None
        if gate["state"] != "setup":
            failed = gate["reasons"][0]
        elif grade["score"] < MIN_GRADE_SCORE:
            failed = f"confluence grade {grade['letter']} below minimum {MIN_GRADE}"

        candidates.append(
            {
                "name": signal["name"],
                "direction": signal["direction"],
                "riskReward": plan["riskReward"],
                "grade": grade["letter"],
                "gradeFactors": grade["factors"],
                "state": "rejected" if failed else "qualified",
                "reason": failed,
            }
        )
        rank = (grade["score"], plan["riskReward"])
        if not failed and (best is None or rank > best[0]):
            best = (rank, signal, plan, grade)

    if best is None:
        return {
            "instrument": underlying,
            "expiry": expiry,
            "view": "Neutral",
            "context": context,
            "candidates": candidates,
            "state": "no-trade",
            "reasons": [c.get("reason") or "strategy rejected" for c in candidates if c["state"] == "rejected"]
            or ["no strategy passed the risk gate"],
        }

    _, signal, plan, grade = best
    for candidate in candidates:
        if candidate["name"] == signal["name"] and candidate["state"] == "qualified":
            candidate["state"] = "selected"

    return {
        "instrument": underlying,
        "expiry": expiry,
        "view": "Bullish" if signal["direction"] == "CE" else "Bearish",
        "signal": signal["name"],
        "grade": grade["letter"],
        "gradeFactors": grade["factors"],
        "confluence": agreement["agree"],
        "context": context,
        "candidates": candidates,
        "trigger": signal["reasons"]
        + oi["reasons"]
        + [f"{volatility['source']} {volatility['value']} ({regime} IV regime)"],
        "entrySpot": spot,
        "state": "setup",
        **plan,
    }


def _grade(signal, agreement, higher_timeframe, oi, spot, plan) -> dict[str, Any]:
    """Confluence score: agreeing strategies, higher-timeframe bias, OI side and room to run."""
    factors: list[str] = []
    score = len(agreement["agree"])
    if score > 1:
        factors.append(f"{score} strategies agree: {', '.join(agreement['agree'])}")
    else:
        factors.append("single strategy signal")

    direction = signal["direction"]
    wanted = "up" if direction == "CE" else "down"
    if higher_timeframe == wanted:
        score += 1
        factors.append(f"{HTF_MINUTES}-minute trend is {higher_timeframe}")
    elif higher_timeframe and higher_timeframe != "flat":
        factors.append(f"{HTF_MINUTES}-minute trend is {higher_timeframe}, against the trade")

    oi_score = oi.get("score", 0)
    if (direction == "CE" and oi_score > 0) or (direction == "PE" and oi_score < 0):
        score += 1
        factors.append(f"open interest score {oi_score} supports the side")

    wall = oi["callWall"] if direction == "CE" else oi["putWall"]
    room = abs(wall - spot)
    needed = 2 * (plan.get("stopDistance") or 0)
    if needed and room >= needed:
        score += 1
        factors.append(f"{round(room, 2)} points to the {direction} wall covers the T2 distance")
    elif needed:
        factors.append(f"only {round(room, 2)} points to the wall, less than the T2 distance")

    letter = "A" if score >= 5 else "B" if score == 4 else "C" if score == 3 else "D"
    return {"score": score, "letter": letter, "factors": factors}


def _risk_gate(setup: dict[str, Any]) -> dict[str, Any]:
    """Option buying is judged purely on reward at T2 against the premium risked to the stop."""
    rr = setup.get("riskReward")
    if rr is None or rr < MIN_RR:
        return {"state": "no-trade", "reasons": [f"risk-reward {rr} below minimum {MIN_RR}"]}
    return {"state": "setup"}


def _build_long_trade(view, regime, chain, spot, atm, step, oi, t, atr, swings):
    if view == "Bullish":
        return _long_option("Long Call (CE)", "CE", chain, spot, atm, step, oi, t, atr, swings)
    if view == "Bearish":
        return _long_option("Long Put (PE)", "PE", chain, spot, atm, step, oi, t, atr, swings)
    return None


def _stop_distance(spot, atr, stop_spot, kind):
    """Honour the strategy's own invalidation level; ATR only floors it and caps absurd values."""
    if not atr or atr <= 0:
        return None
    structural = abs(spot - stop_spot) if stop_spot else STOP_ATR_MIN * atr
    return round(min(max(STOP_ATR_MIN * atr, structural), STOP_ATR_MAX * atr), 2)


def _select_long_leg(chain, spot, atm, step, kind, t):
    """Pick the liquid near-the-money strike whose delta is closest to the target."""
    best = None
    for offset in range(-3, 4):
        strike = round(atm + offset * step, 2)
        leg = _leg("BUY", strike, kind, chain, spot, t)
        if leg is None or leg.get("delta") is None or not leg.get("oi"):
            continue
        score = abs(abs(leg["delta"]) - TARGET_DELTA)
        if best is None or score < best[0]:
            best = (score, leg)
    return best[1] if best else None


def _reprice(leg, target_spot, t):
    iv = (leg.get("iv") or 0) / 100
    if iv <= 0:
        return None
    return round(option_price(target_spot, leg["strike"], t, RISK_FREE_RATE, iv, leg["optionType"]), 2)


def _long_option(name, kind, chain, spot, atm, step, oi, t, atr, stop_spot):
    leg = _select_long_leg(chain, spot, atm, step, kind, t)
    distance = _stop_distance(spot, atr, stop_spot, kind)
    if leg is None or distance is None or distance <= 0:
        return None

    direction = 1 if kind == "CE" else -1
    entry = leg["ltp"]
    lot = leg.get("lotSize") or 1
    stop_spot = round(spot - direction * distance, 2)

    modelled_stop = _reprice(leg, stop_spot, t)
    if modelled_stop is None:
        return None
    # Keep the stop at the structural level; if that risks too much premium, skip the trade
    # rather than tightening into noise.
    stop_premium = modelled_stop
    risk_fraction = (entry - stop_premium) / entry if entry else 1.0
    if risk_fraction > MAX_PREMIUM_RISK:
        return None
    risk_per_lot = round((entry - stop_premium) * lot, 2)
    if risk_per_lot <= 0:
        return None

    targets = []
    for index, multiple in enumerate((1.0, 2.0, 3.0), start=1):
        target_spot = round(spot + direction * multiple * distance, 2)
        premium = _reprice(leg, target_spot, t)
        if premium is None:
            return None
        targets.append(
            {
                "label": f"T{index}",
                "spot": target_spot,
                "netPremium": premium,
                "profitPerLot": round((premium - entry) * lot, 2),
            }
        )

    wall = oi["callWall"] if kind == "CE" else oi["putWall"]
    return {
        "strategy": name,
        "structure": f"buy {kind} only",
        "legs": [leg],
        "netPremium": entry,
        "premiumType": "debit",
        "lotSize": lot,
        "stopDistance": distance,
        "positionGreeks": _position_greeks([leg]),
        "stopLoss": {
            "spot": stop_spot,
            "netPremium": stop_premium,
            "note": f"exit if spot closes beyond {stop_spot} or premium drops to {stop_premium}",
        },
        "targets": targets,
        "maxRiskPerLot": risk_per_lot,
        "maxRewardPerLot": targets[-1]["profitPerLot"],
        "premiumAtRiskPerLot": round(entry * lot, 2),
        "riskReward": round(targets[1]["profitPerLot"] / risk_per_lot, 2) if risk_per_lot > 0 else None,
        "adjustmentPlan": [
            f"Book one third at T1 ({targets[0]['spot']}) and move the stop to entry premium {entry}.",
            f"Trail the remainder behind each new swing; {wall} is the nearest OI wall to watch.",
            "Never average a losing long option; time decay works against you every minute.",
            "Exit by 15:15 IST, and avoid fresh buying in the last 30 minutes on expiry day.",
        ],
    }
