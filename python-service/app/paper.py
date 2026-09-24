from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

ENTRY_BAND = float(os.getenv("PAPER_ENTRY_BAND", "0.02"))
MAX_POSITIONS_PER_INSTRUMENT = int(os.getenv("PAPER_MAX_POSITIONS", "1"))
MAX_OPEN_POSITIONS = int(os.getenv("PAPER_MAX_OPEN", "2"))
ALLOWED_GRADES = {g.strip().upper() for g in os.getenv("PAPER_ALLOWED_GRADES", "A").split(",") if g.strip()}
COOLDOWN_MINUTES = float(os.getenv("PAPER_COOLDOWN_MINUTES", "0"))
MAX_TRADES_PER_DAY = int(os.getenv("PAPER_MAX_TRADES_PER_DAY", "10"))
DAILY_LOSS_LIMIT = float(os.getenv("PAPER_DAILY_LOSS_LIMIT", "6000"))
SQUARE_OFF_TIME = os.getenv("PAPER_SQUARE_OFF", "15:15")
GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


def _store_path() -> Path:
    default = Path(__file__).resolve().parent.parent / ".cache" / "paper-positions.json"
    return Path(os.getenv("PAPER_STORE", default))


class PaperBook:
    """Dummy fills only: one lot per qualified setup, no broker order is ever sent."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._open: list[dict[str, Any]] = []
        self._closed: list[dict[str, Any]] = []
        self._sequence = 1
        self._day: str | None = None
        self._trades_today = 0
        self._last_exit: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        path = _store_path()
        if not path.exists():
            return
        try:
            saved = json.loads(path.read_text())
            self._open = saved.get("open", [])
            self._closed = saved.get("closed", [])
            self._sequence = saved.get("sequence", 1)
        except (OSError, ValueError) as error:
            logger.warning("Could not read paper book: %s", error)

    def _save(self) -> None:
        path = _store_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"open": self._open, "closed": self._closed, "sequence": self._sequence})
            )
        except OSError as error:
            logger.warning("Could not persist paper book: %s", error)

    def update(self, setups: list[dict[str, Any]], latest: dict[str, dict[str, Any]], now: datetime) -> None:
        with self._lock:
            changed = self._open_new(setups, latest, now)
            changed = self._mark_to_market(latest, now) or changed
            if changed:
                self._save()

    def _open_new(self, setups, latest, now) -> bool:
        changed = False
        day = now.date().isoformat()
        if self._day != day:
            self._day = day
            self._trades_today = 0

        if self._trades_today >= MAX_TRADES_PER_DAY:
            return False
        if self._booked_today(day) <= -abs(DAILY_LOSS_LIMIT):
            return False

        # The daily budget is small, so spend it on the best graded setup available, not the first seen.
        ranked = sorted(
            (s for s in setups if s.get("state") == "setup"),
            key=lambda s: (GRADE_RANK.get((s.get("grade") or "").upper(), 9), -(s.get("riskReward") or 0)),
        )
        for setup in ranked:
            if ALLOWED_GRADES and (setup.get("grade") or "").upper() not in ALLOWED_GRADES:
                continue
            legs = [leg for leg in setup.get("legs") or [] if leg.get("token")]
            if not legs:
                continue
            instrument = setup.get("instrument")
            if self._in_cooldown(instrument, now):
                continue
            live = [self._price(latest, leg) for leg in legs]
            if any(price is None for price in live):
                continue

            held = [p for p in self._open if p["instrument"] == instrument]
            if len(held) >= MAX_POSITIONS_PER_INSTRUMENT:
                continue
            # Without a book-wide cap, adding stock underlyings would open one trade each.
            if len(self._open) >= MAX_OPEN_POSITIONS:
                continue
            signature = self._signature(legs)
            if any(p["signature"] == signature for p in self._open):
                continue

            suggested = float(setup.get("netPremium") or 0)
            combined = round(sum(price * leg.get("quantity", 1) for price, leg in zip(live, legs)), 2)
            if suggested <= 0:
                continue
            low, high = suggested * (1 - ENTRY_BAND), suggested * (1 + ENTRY_BAND)
            if not (low <= combined <= high):
                continue

            lot = int(setup.get("lotSize") or 1)
            position = {
                "id": self._sequence,
                "signature": signature,
                "instrument": instrument,
                "strategy": setup.get("strategy"),
                "signal": setup.get("signal"),
                "grade": setup.get("grade"),
                "confluence": setup.get("confluence") or [],
                "view": setup.get("view"),
                "expiry": setup.get("expiry"),
                "lots": 1,
                "lotSize": lot,
                "legs": [
                    {
                        "token": str(leg["token"]),
                        "symbol": leg.get("symbol"),
                        "strike": leg.get("strike"),
                        "optionType": leg.get("optionType"),
                        "quantity": leg.get("quantity", 1),
                        "entry": price,
                    }
                    for leg, price in zip(legs, live)
                ],
                "suggestedEntry": round(suggested, 2),
                "entryPremium": combined,
                "entryAt": now.isoformat(),
                "stopPremium": (setup.get("stopLoss") or {}).get("netPremium"),
                "targets": [
                    {"label": t.get("label"), "premium": t.get("netPremium"), "hit": False, "hitAt": None}
                    for t in setup.get("targets") or []
                ],
                "lastPremium": combined,
                "pnl": 0.0,
                "pnlPercent": 0.0,
                "best": 0.0,
                "worst": 0.0,
                "status": "OPEN",
            }
            self._open.append(position)
            self._sequence += 1
            self._trades_today += 1
            changed = True
            logger.info("Paper fill: %s %s at %s", instrument, setup.get("strategy"), combined)
            if self._trades_today >= MAX_TRADES_PER_DAY or len(self._open) >= MAX_OPEN_POSITIONS:
                break
        return changed

    def _booked_today(self, day: str) -> float:
        return sum(
            (p.get("closePremium", 0) - p["entryPremium"]) * p["lotSize"] * p["lots"]
            for p in self._closed
            if str(p.get("closedAt", ""))[:10] == day
        )

    def _in_cooldown(self, instrument: str, now: datetime) -> bool:
        """A fresh signal fires every candle, so re-entry is paused after each exit."""
        last = self._last_exit.get(instrument)
        if not last or COOLDOWN_MINUTES <= 0:
            return False
        try:
            closed_at = datetime.fromisoformat(last)
        except ValueError:
            return False
        return (now - closed_at).total_seconds() < COOLDOWN_MINUTES * 60

    def _mark_to_market(self, latest, now) -> bool:
        changed = False
        still_open = []
        for position in self._open:
            prices = [self._price(latest, leg) for leg in position["legs"]]
            if any(price is None for price in prices):
                still_open.append(position)
                continue

            combined = round(
                sum(price * leg.get("quantity", 1) for price, leg in zip(prices, position["legs"])), 2
            )
            lot = position["lotSize"] * position["lots"]
            pnl = round((combined - position["entryPremium"]) * lot, 2)
            position["lastPremium"] = combined
            position["pnl"] = pnl
            position["pnlPercent"] = (
                round((combined / position["entryPremium"] - 1) * 100, 2) if position["entryPremium"] else 0.0
            )
            position["best"] = max(position.get("best", 0.0), pnl)
            position["worst"] = min(position.get("worst", 0.0), pnl)

            for target in position["targets"]:
                if not target["hit"] and target["premium"] and combined >= target["premium"]:
                    target["hit"] = True
                    target["hitAt"] = now.isoformat()
                    changed = True

            targets = position["targets"]
            hits = sum(1 for t in targets if t["hit"])
            stop = position.get("stopPremium") or 0
            # Reaching a target must protect the gain, otherwise a +2R trade can round-trip to -1R.
            if hits >= 2 and targets[0].get("premium"):
                stop = max(stop, targets[0]["premium"])
            elif hits >= 1:
                stop = max(stop, position["entryPremium"])
            position["activeStop"] = round(stop, 2)

            final_target = targets[-1]["premium"] if targets else None
            if SQUARE_OFF_TIME and now.strftime("%H:%M") >= SQUARE_OFF_TIME:
                self._close(position, combined, now, f"time exit {SQUARE_OFF_TIME}")
                changed = True
                continue
            if stop and combined <= stop:
                reason = "trailing stop" if hits else "stop loss hit"
                self._close(position, combined, now, reason)
                changed = True
                continue
            if final_target and combined >= final_target:
                self._close(position, combined, now, "final target hit")
                changed = True
                continue
            still_open.append(position)

        self._open = still_open
        return changed

    def _close(self, position, price, now, reason) -> None:
        position["status"] = "CLOSED"
        position["closePremium"] = price
        position["closedAt"] = now.isoformat()
        position["exitReason"] = reason
        self._last_exit[position["instrument"]] = now.isoformat()
        self._closed.insert(0, position)
        self._closed = self._closed[:50]
        logger.info("Paper exit: %s at %s (%s)", position["instrument"], price, reason)

    @staticmethod
    def _price(latest, leg) -> float | None:
        quote = latest.get(str(leg.get("token")))
        price = quote.get("ltp") if quote else None
        return float(price) if price else None

    @staticmethod
    def _signature(legs) -> str:
        return "|".join(sorted(f"{leg.get('token')}" for leg in legs))

    def close_position(self, position_id: int, now: datetime) -> dict[str, Any]:
        """Manual exit at the last marked premium; the cooldown then blocks an immediate re-entry."""
        with self._lock:
            for position in list(self._open):
                if position["id"] == position_id:
                    price = position.get("lastPremium") or position["entryPremium"]
                    self._open.remove(position)
                    self._close(position, price, now, "manual exit")
                    self._save()
                    return {"closed": True, "id": position_id, "price": price, "pnl": position.get("pnl")}
        return {"closed": False, "id": position_id, "error": "position not found"}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            open_pnl = round(sum(p["pnl"] for p in self._open), 2)
            booked = round(
                sum((p.get("closePremium", 0) - p["entryPremium"]) * p["lotSize"] * p["lots"] for p in self._closed),
                2,
            )
            wins = sum(1 for p in self._closed if p.get("closePremium", 0) > p["entryPremium"])
            return {
                "open": [dict(p) for p in self._open],
                "closed": [dict(p) for p in self._closed],
                "byStrategy": self._breakdown("signal"),
                "byGrade": self._breakdown("grade"),
                "byExpiry": self._breakdown("expiry"),
                "summary": {
                    "openCount": len(self._open),
                    "closedCount": len(self._closed),
                    "openPnl": open_pnl,
                    "bookedPnl": booked,
                    "totalPnl": round(open_pnl + booked, 2),
                    "wins": wins,
                    "losses": len(self._closed) - wins,
                    "hitRate": round(100 * wins / len(self._closed), 1) if self._closed else None,
                },
            }

    def _breakdown(self, key: str) -> list[dict[str, Any]]:
        """Closed-trade outcomes per strategy or grade, so confluence can be judged on evidence."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for position in self._closed:
            groups.setdefault(position.get(key) or "unknown", []).append(position)
        rows = []
        for name, positions in groups.items():
            wins = sum(1 for p in positions if p.get("closePremium", 0) > p["entryPremium"])
            pnl = sum((p.get("closePremium", 0) - p["entryPremium"]) * p["lotSize"] * p["lots"] for p in positions)
            rows.append(
                {
                    "name": name,
                    "trades": len(positions),
                    "wins": wins,
                    "losses": len(positions) - wins,
                    "hitRate": round(100 * wins / len(positions), 1),
                    "pnl": round(pnl, 2),
                }
            )
        return sorted(rows, key=lambda row: row["pnl"], reverse=True)
