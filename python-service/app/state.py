from __future__ import annotations

import os
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import pandas as pd

from .chain import build_chain, expiries_for
from .greeks import ist_now
from .indicators import session_vwap
from .paper import PaperBook
from .strategy import evaluate_underlying

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
SERIES_TYPES = {"SPOT", "FUT"}
STALE_FEED_SECONDS = float(os.getenv("STALE_FEED_SECONDS", "90"))
SNAPSHOT_TTL_SECONDS = float(os.getenv("SNAPSHOT_TTL_SECONDS", "1"))


GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


def _confirmed(candles: pd.DataFrame, interval_seconds: int) -> pd.DataFrame:
    """Drop the candle still forming; acting on it means trading unconfirmed wicks."""
    if candles.empty:
        return candles
    current_bucket = pd.Timestamp.now(tz="UTC").floor(f"{interval_seconds}s")
    if pd.Timestamp(candles["timestamp"].iloc[-1]) >= current_bucket:
        return candles.iloc[:-1]
    return candles


def _best_setup(evaluated: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer a qualifying setup by grade then risk-reward; otherwise report the nearest expiry."""
    qualified = [s for s in evaluated if s.get("state") == "setup"]
    if qualified:
        return min(
            qualified,
            key=lambda s: (GRADE_RANK.get((s.get("grade") or "").upper(), 9), -(s.get("riskReward") or 0)),
        )
    return evaluated[0]


def _token_for(metadata: dict[str, dict[str, Any]], instrument_type: str, underlying: str | None = None) -> str | None:
    matches = [
        token
        for token, meta in metadata.items()
        if meta.get("instrumentType") == instrument_type
        and (underlying is None or meta.get("underlying") == underlying or meta.get("symbol") == underlying)
    ]
    if not matches:
        return None
    if instrument_type != "FUT":
        return matches[0]
    return min(matches, key=lambda token: metadata[token].get("expiry") or "9999")


class MarketState:
    def __init__(self, interval_seconds: int = 60) -> None:
        self.interval_seconds = interval_seconds
        self._lock = Lock()
        self._latest_by_token: dict[str, dict[str, Any]] = {}
        self._metadata_by_token: dict[str, dict[str, Any]] = {}
        self._oi_baseline: dict[str, float] = {}
        self._bucket_base_volume: dict[str, float] = {}
        self._last_cumulative_volume: dict[str, float] = {}
        self._candles_by_token: dict[str, pd.DataFrame] = {}
        self._last_tick_at: datetime | None = None
        self._watch_tokens: set[str] = set()
        self._seeded_tokens: set[str] = set()
        self._cache: dict[str, Any] | None = None
        self._cache_at: datetime | None = None
        self._paper = PaperBook()
        self._status = "starting"
        self._last_error: str | None = None

    def set_status(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._last_error = error

    def seed_candles(self, token: str, instrument: dict[str, Any], rows: list[list[Any]]) -> None:
        """Merge broker history in front of any candles already built from live ticks."""
        history = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp(row[0]).tz_convert("UTC"),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5] or 0),
                    "oi": None,
                }
                for row in rows
                if row and len(row) >= 6
            ],
            columns=CANDLE_COLUMNS,
        )
        if history.empty:
            return

        with self._lock:
            self._metadata_by_token.setdefault(
                token,
                {
                    key: instrument.get(key)
                    for key in ("symbol", "instrumentType", "expiry", "strike", "optionType", "lotSize")
                    if instrument.get(key) is not None
                }
                | {"underlying": instrument.get("name")},
            )
            existing = self._candles_by_token.get(token)
            if existing is not None and not existing.empty:
                history = history[history["timestamp"] < existing["timestamp"].min()]
                merged = pd.concat([history, existing], ignore_index=True)
            else:
                merged = history
            self._candles_by_token[token] = merged.tail(400).reset_index(drop=True)
            self._cache = None

    def add_tick(self, tick: dict[str, Any]) -> None:
        price = float(tick.get("ltp", 0) or 0)
        if price <= 0:
            return
        token = str(tick.get("token") or tick.get("symbol") or "unknown")
        timestamp = tick.get("timestamp") or datetime.now(timezone.utc).isoformat()
        open_interest = tick.get("oi")
        oi_change = tick.get("oiChange")

        with self._lock:
            if open_interest is not None:
                # SmartAPI's OI-change field is documented as garbage, so track drift from the first tick seen.
                baseline = self._oi_baseline.setdefault(token, float(open_interest))
                oi_change = float(open_interest) - baseline

            self._metadata_by_token[token] = {
                key: tick.get(key)
                for key in ("symbol", "underlying", "instrumentType", "assetClass", "expiry", "strike", "optionType", "lotSize")
                if tick.get(key) is not None
            }
            self._latest_by_token[token] = {
                "timestamp": timestamp,
                "ltp": price,
                "volume": tick.get("volume", 0),
                "oi": open_interest,
                "oiChange": oi_change,
            }
            self._last_tick_at = datetime.now(timezone.utc)
            if tick.get("instrumentType") in SERIES_TYPES or token in self._watch_tokens:
                self._update_candle(token, timestamp, price, tick.get("volume", 0), open_interest)

    def _update_candle(self, token: str, timestamp: str, price: float, cumulative_volume: Any, oi: Any) -> None:
        bucket = pd.Timestamp(timestamp, tz="UTC").floor(f"{self.interval_seconds}s")
        candles = self._candles_by_token.setdefault(token, pd.DataFrame(columns=CANDLE_COLUMNS))
        # The feed reports volume cumulatively for the day; candles need the per-bucket delta,
        # measured from the previous tick so volume between candles is not lost.
        cumulative = float(cumulative_volume or 0)
        previous = self._last_cumulative_volume.get(token)
        if candles.empty or candles.iloc[-1]["timestamp"] != bucket:
            self._bucket_base_volume[token] = cumulative if previous is None else previous
            candles.loc[len(candles)] = {
                "timestamp": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": max(0.0, cumulative - self._bucket_base_volume[token]),
                "oi": oi,
            }
        else:
            index = candles.index[-1]
            base = self._bucket_base_volume.setdefault(token, cumulative)
            candles.loc[index, "high"] = max(candles.loc[index, "high"], price)
            candles.loc[index, "low"] = min(candles.loc[index, "low"], price)
            candles.loc[index, "close"] = price
            candles.loc[index, "volume"] = max(0.0, cumulative - base)
            candles.loc[index, "oi"] = oi
        self._last_cumulative_volume[token] = cumulative
        self._candles_by_token[token] = candles.tail(200).reset_index(drop=True)

    def _token_for_role(self, *args, **kwargs):
        return _token_for(*args, **kwargs)

    def pending_history(self) -> list[tuple[str, dict[str, Any]]]:
        """Watched option strikes whose intraday candles have not been backfilled yet."""
        with self._lock:
            return [
                (token, dict(self._metadata_by_token.get(token, {})))
                for token in self._watch_tokens - self._seeded_tokens
                if token in self._metadata_by_token
            ]

    def mark_seeded(self, token: str) -> None:
        with self._lock:
            self._seeded_tokens.add(token)

    def paper_snapshot(self) -> dict[str, Any]:
        return self._paper.snapshot()

    def close_paper_position(self, position_id: int) -> dict[str, Any]:
        return self._paper.close_position(position_id, ist_now())

    def snapshot(self, symbol: str, exchange: str) -> dict[str, Any]:
        now = ist_now()
        with self._lock:
            if self._cache and self._cache_at and (now - self._cache_at).total_seconds() < SNAPSHOT_TTL_SECONDS:
                return self._cache
            # Copy references cheaply, then release the lock so tick ingestion is never
            # blocked by indicator, chain and Black-Scholes work.
            latest = dict(self._latest_by_token)
            metadata = dict(self._metadata_by_token)
            candles_by_token = {token: frame for token, frame in self._candles_by_token.items()}
            status, error, last_tick_at = self._status, self._last_error, self._last_tick_at

        payload = self._compute(symbol, exchange, now, latest, metadata, candles_by_token, status, error, last_tick_at)
        with self._lock:
            for setup in payload["setups"]:
                for leg in setup.get("legs") or []:
                    if leg.get("token"):
                        self._watch_tokens.add(str(leg["token"]))
            self._cache, self._cache_at = payload, now
        return payload

    def _compute(self, symbol, exchange, now, latest, metadata, candles_by_token, status, error, last_tick_at):
        vix_token = _token_for(metadata, "VIX")
        vix = latest.get(vix_token, {}).get("ltp") if vix_token else None
        underlyings = sorted(
            {
                meta.get("underlying") or meta.get("symbol")
                for meta in metadata.values()
                if meta.get("instrumentType") == "SPOT"
            }
        )

        setups = []
        views = {}
        for name in underlyings:
            spot_token = _token_for(metadata, "SPOT", name)
            futures_token = _token_for(metadata, "FUT", name)
            spot = latest.get(spot_token, {}).get("ltp") if spot_token else None
            spot_candles = candles_by_token.get(spot_token, pd.DataFrame(columns=CANDLE_COLUMNS))
            futures_candles = candles_by_token.get(futures_token, pd.DataFrame(columns=CANDLE_COLUMNS))
            expiries = expiries_for(metadata, name)

            # Chart only the current session; seeded history spans days and would show gaps.
            chart = spot_candles
            if not chart.empty:
                stamps = pd.to_datetime(chart["timestamp"], utc=True)
                chart = chart[stamps.dt.date == stamps.max().date()]

            # Signals need volume for VWAP: index spot has none, so futures stand in.
            technical_candles = spot_candles
            if not futures_candles.empty and futures_candles["volume"].astype(float).sum() > 0:
                if spot_candles.empty or spot_candles["volume"].astype(float).sum() <= 0:
                    technical_candles = futures_candles

            vwap = session_vwap(technical_candles)
            technical_candles = _confirmed(technical_candles, self.interval_seconds)
            asset_class = metadata.get(spot_token, {}).get("assetClass") or "INDEX"
            # Each expiry is a separate chain, so they are graded independently and compared.
            evaluated = [
                evaluate_underlying(
                    underlying=name,
                    expiry=expiry,
                    spot=spot,
                    spot_candles=technical_candles,
                    futures_vwap=vwap,
                    chain=build_chain(metadata, latest, name, expiry) if expiry else {},
                    vix=vix,
                    now=now,
                    asset_class=asset_class,
                )
                for expiry in (expiries or [None])
            ]
            setup = _best_setup(evaluated)
            setup["expiriesScanned"] = expiries
            chain = build_chain(metadata, latest, name, setup.get("expiry")) if setup.get("expiry") else {}
            # The plan is priced in premium, so the chart follows the selected strike.
            primary = (setup.get("legs") or [{}])[0]
            option_token = primary.get("token")
            option_candles = candles_by_token.get(option_token) if option_token else None
            setup["chart"] = {
                "symbol": metadata.get(option_token, {}).get("symbol") if option_token else None,
                "candles": [
                    {**row, "timestamp": row["timestamp"].isoformat()}
                    for row in option_candles.tail(80).to_dict(orient="records")
                ]
                if option_candles is not None and not option_candles.empty
                else [],
            }
            setups.append(setup)
            views[name] = {
                "spot": spot,
                "expiry": setup.get("expiry"),
                "expiriesScanned": expiries,
                "chainStrikes": len(chain),
                "candles": [
                    {**row, "timestamp": row["timestamp"].isoformat()}
                    for row in chart.tail(80).to_dict(orient="records")
                ],
            }

        feed_age = (datetime.now(timezone.utc) - last_tick_at).total_seconds() if last_tick_at else None
        if status == "live" and (feed_age is None or feed_age > STALE_FEED_SECONDS):
            status = "stale"

        self._paper.update(setups, latest, now)

        return {
            "status": status,
            "error": error,
            "generatedAt": now.isoformat(),
            "symbol": symbol,
            "exchange": exchange,
            "vix": vix,
            "paper": self._paper.snapshot()["summary"],
            "feedAgeSeconds": round(feed_age, 1) if feed_age is not None else None,
            "trackedInstruments": len(metadata),
            "underlyings": views,
            "setups": setups,
        }
