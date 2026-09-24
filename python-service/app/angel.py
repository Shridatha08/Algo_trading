from __future__ import annotations

import logging
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Any, Callable

import pyotp
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from .config import Settings
from .chain import EXPIRY_DEPTH, tradable_expiries
from .greeks import ist_now
from .instruments import EXCHANGE_TYPES, load_market_universe

logger = logging.getLogger(__name__)

HISTORY_INTERVALS = {
    60: "ONE_MINUTE",
    180: "THREE_MINUTE",
    300: "FIVE_MINUTE",
    600: "TEN_MINUTE",
    900: "FIFTEEN_MINUTE",
    1800: "THIRTY_MINUTE",
    3600: "ONE_HOUR",
}
HISTORY_RATE_DELAY = float(os.getenv("HISTORY_RATE_DELAY", "2.5"))
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "10"))
MAX_SUBSCRIPTIONS = int(os.getenv("MAX_SUBSCRIPTIONS", "500"))
# NIFTY's full front-expiry chain is ~91 strikes, so this window covers it with no blind spots.
STRIKE_WINDOW = int(os.getenv("STRIKE_WINDOW", "45"))


class AngelMarketData:
    def __init__(self, settings: Settings, on_tick: Callable[[dict[str, Any]], None]) -> None:
        self.settings = settings
        self.on_tick = on_tick
        self.smart_api: SmartConnect | None = None
        self.websocket: SmartWebSocketV2 | None = None
        self.feed_token = ""
        self.instruments: list[dict[str, Any]] = []
        self._instrument_by_token: dict[str, dict[str, Any]] = {}
        self._history_attempts: dict[str, int] = {}
        self._pending_series: list[dict[str, Any]] = []
        self._core: list[dict[str, Any]] = []
        self._options: list[dict[str, Any]] = []
        self._last_spot: dict[str, float] = {}
        self.series_seeded = False

    def verify_credentials(self) -> dict[str, Any]:
        """Authenticate without opening a market-data WebSocket."""
        if not self.settings.credentials_ready:
            raise RuntimeError(
                "ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, and ANGEL_TOTP_SECRET are required"
            )
        self.smart_api = SmartConnect(api_key=self.settings.api_key)
        session = self.smart_api.generateSession(
            self.settings.client_code,
            self.settings.pin,
            pyotp.TOTP(self.settings.totp_secret).now(),
        )
        if not session.get("status"):
            raise RuntimeError("SmartAPI login failed; check credentials, TOTP clock, and account access")
        self.feed_token = self.smart_api.getfeedToken()
        return {"authenticated": True, "feedTokenReceived": bool(self.feed_token)}

    def start(self) -> None:
        if not self.settings.credentials_ready:
            raise RuntimeError("AngelOne credentials are required for live mode")
        subscribe_all = os.getenv("SUBSCRIBE_ALL_OPTIONS", "true").lower() in {"1", "true", "yes"}
        if subscribe_all:
            url = os.getenv(
                "SCRIP_MASTER_URL",
                "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
            )
            names = os.getenv("OPTION_UNDERLYINGS", "NIFTY").split(",")
            stocks = os.getenv("STOCK_UNDERLYINGS", "").split(",")
            self.instruments = load_market_universe(url, names, stocks)
        elif self.settings.token:
            self.instruments = [
                {
                    "token": self.settings.token,
                    "symbol": self.settings.symbol,
                    "name": self.settings.symbol,
                    "instrumentType": "OPT",
                    "exchangeType": EXCHANGE_TYPES.get(self.settings.exchange.upper(), 2),
                }
            ]
        else:
            raise RuntimeError("Set TOKEN or enable SUBSCRIBE_ALL_OPTIONS")
        if not self.instruments:
            raise RuntimeError("No live instruments found")

        # Only the front-month future is ever read, so far-dated contracts are dropped
        # before they consume the session's token quota.
        nearest_futures: dict[str, dict[str, Any]] = {}
        for item in self.instruments:
            if item["instrumentType"] != "FUT":
                continue
            current = nearest_futures.get(item["name"])
            if current is None or (item.get("expiry") or "9999") < (current.get("expiry") or "9999"):
                nearest_futures[item["name"]] = item
        front_tokens = {item["token"] for item in nearest_futures.values()}
        self.instruments = [
            item for item in self.instruments if item["instrumentType"] != "FUT" or item["token"] in front_tokens
        ]

        # Options are subscribed later, windowed around ATM, so the 1000-token session
        # quota is spent on strikes the engine can actually trade.
        self._core = [i for i in self.instruments if i["instrumentType"] in ("SPOT", "VIX", "FUT")]
        self._options = [i for i in self.instruments if i["instrumentType"] == "OPT"]
        self._instrument_by_token = {item["token"]: item for item in self.instruments}
        if len(self._core) > MAX_SUBSCRIPTIONS:
            raise RuntimeError(
                f"{len(self._core)} spot/futures tokens exceed the {MAX_SUBSCRIPTIONS} quota; reduce underlyings"
            )

        self.verify_credentials()
        auth_token = self.smart_api.access_token
        self.websocket = SmartWebSocketV2(
            auth_token,
            self.settings.api_key,
            self.settings.client_code,
            self.feed_token,
            max_retry_attempt=int(os.getenv("FEED_MAX_RETRY", "5")),
        )
        self.websocket.on_open = self._on_open
        self.websocket.on_data = self._on_data
        self.websocket.on_error = self._on_error
        self.websocket.on_close = self._on_close
        Thread(target=self.websocket.connect, daemon=True).start()

    def _on_open(self, websocket: Any) -> None:
        logger.info("SmartAPI WebSocket connected")
        self._subscribe(self._core, "core")
        counts = Counter(item.get("instrumentType") for item in self._core)
        logger.info("Subscribed core: %s; waiting for spot before selecting strikes", dict(counts))
        Thread(target=self._subscribe_option_windows, daemon=True).start()

    def _subscribe(self, instruments: list[dict[str, Any]], label: str) -> int:
        by_exchange: dict[int, list[str]] = {}
        for instrument in instruments:
            by_exchange.setdefault(instrument["exchangeType"], []).append(instrument["token"])
        batch = 0
        for exchange_type, tokens in by_exchange.items():
            for start in range(0, len(tokens), 1000):
                self.websocket.subscribe(
                    f"{label}-{batch}",
                    3,
                    [{"exchangeType": exchange_type, "tokens": tokens[start : start + 1000]}],
                )
                batch += 1
        return len(instruments)

    def _subscribe_option_windows(self) -> None:
        """Pick strikes around each underlying's live spot so the quota covers many symbols."""
        expected = {i["name"] for i in self._core if i["instrumentType"] == "SPOT"}
        deadline = time.time() + 90
        while time.time() < deadline and not expected <= set(self._last_spot):
            time.sleep(2)

        ready = {name: price for name, price in self._last_spot.items() if price > 0}
        missing = expected - set(ready)
        if missing:
            logger.warning("No spot yet for %s; their options are skipped", ", ".join(sorted(missing)))
        if not ready:
            logger.error("No spot prices received; cannot select strikes")
            return

        by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for option in self._options:
            if option["name"] in ready and option.get("strike"):
                by_name[option["name"]].append(option)

        budget = MAX_SUBSCRIPTIONS - len(self._core)
        per_name = max(20, budget // max(1, len(by_name)))
        selected: list[dict[str, Any]] = []
        for name, options in by_name.items():
            expiries = tradable_expiries({option["expiry"] for option in options})[:EXPIRY_DEPTH]
            if not expiries:
                continue
            chosen: list[dict[str, Any]] = []
            # Window each expiry separately so the nearer one cannot crowd out the next.
            for expiry in expiries:
                bucket = [option for option in options if option["expiry"] == expiry]
                strikes = sorted({o["strike"] for o in bucket}, key=lambda s: abs(s - ready[name]))
                window = set(strikes[: STRIKE_WINDOW * 2 + 1])
                chosen.extend(
                    sorted(
                        (o for o in bucket if o["strike"] in window),
                        key=lambda o: (abs(o["strike"] - ready[name]), o["optionType"]),
                    )
                )
            selected.extend(chosen[:per_name])

        selected = selected[:budget]
        self._subscribe(selected, "strikes")
        logger.info(
            "Subscribed %d strikes across %d underlyings (%d/%d tokens used)",
            len(selected),
            len(by_name),
            len(self._core) + len(selected),
            MAX_SUBSCRIPTIONS,
        )

    def _on_data(self, websocket: Any, message: dict[str, Any]) -> None:
        # SmartAPI field names can differ by feed mode; normalize the fields used by the app.
        token = str(message.get("token", ""))
        instrument = self._instrument_by_token.get(token, {})
        price = message.get("last_traded_price", 0) / 100
        if instrument.get("instrumentType") == "SPOT" and price > 0:
            self._last_spot[instrument["name"]] = price
        self.on_tick(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "token": token,
                "symbol": instrument.get("symbol"),
                "underlying": instrument.get("name"),
                "instrumentType": instrument.get("instrumentType"),
                "assetClass": instrument.get("assetClass"),
                "expiry": instrument.get("expiry"),
                "strike": instrument.get("strike"),
                "optionType": instrument.get("optionType"),
                "lotSize": instrument.get("lotSize"),
                "ltp": price,
                "volume": message.get("volume_trade_for_the_day", 0),
                "oi": message.get("open_interest"),
                # SmartAPI documents open_interest_change as a dummy field, so OI drift is derived locally.
            }
        )

    def _on_error(self, websocket: Any, error: Any) -> None:
        logger.error("SmartAPI WebSocket error: %s", error)

    def _on_close(self, websocket: Any) -> None:
        logger.warning("SmartAPI WebSocket closed")

    def fetch_oi(self) -> dict[str, Any] | None:
        if self.smart_api is None:
            return None
        # Keep OI polling behind this adapter so the future option-chain selection logic
        # can be added without changing the API or dashboard.
        return None

    def _fetch_candles(self, exchange: str, token: str, interval: str, start, end) -> list[Any]:
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": start.strftime("%Y-%m-%d %H:%M"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }
        for attempt in range(4):
            try:
                response = self.smart_api.getCandleData(params)
                rows = (response or {}).get("data") or []
                if rows:
                    return rows
            except Exception as error:
                logger.warning("History attempt %d for token %s failed: %s", attempt + 1, token, error)
            time.sleep(HISTORY_RATE_DELAY * (attempt + 2))
        return []

    def seed_watched_options(self, state: Any, interval_seconds: int = 60) -> int:
        """Backfill today's candles for strikes the engine has selected, so charts are not empty."""
        if self.smart_api is None:
            return 0
        pending = state.pending_history()
        if not pending:
            return 0
        interval = HISTORY_INTERVALS.get(interval_seconds, "ONE_MINUTE")
        now = ist_now()
        session_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now <= session_open:
            return 0

        seeded = 0
        for token, meta in pending:
            if meta.get("instrumentType") != "OPT":
                state.mark_seeded(token)
                continue
            attempts = self._history_attempts.get(token, 0)
            if attempts >= 3:
                state.mark_seeded(token)
                logger.warning("Giving up on history for %s; chart will build from live ticks", meta.get("symbol"))
                continue
            rows = self._fetch_candles("NFO", token, interval, session_open, now)
            if rows:
                state.seed_candles(token, {**meta, "name": meta.get("underlying")}, rows)
                state.mark_seeded(token)
                seeded += len(rows)
                logger.info("Seeded %d candles for strike %s", len(rows), meta.get("symbol"))
            else:
                # Leave it unseeded so the next background pass retries after the throttle clears.
                self._history_attempts[token] = attempts + 1
            time.sleep(HISTORY_RATE_DELAY)
        return seeded

    def seed_history(self, state: Any, interval_seconds: int = 60) -> int:
        """Backfill today's index and futures candles so EMA/ATR/VWAP work from the first tick."""
        if self.smart_api is None:
            return 0
        interval = HISTORY_INTERVALS.get(interval_seconds, "ONE_MINUTE")
        now = ist_now()
        session_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now <= session_open:
            # Started pre-market: stay unseeded so the background loop retries after the open.
            logger.info("Pre-open; history seed deferred until %s", session_open.strftime("%H:%M"))
            return 0
        # EMAs need far more bars than one session provides at 5-minute candles,
        # so pull several sessions; VWAP re-anchors to the latest session downstream.
        history_start = (now - timedelta(days=HISTORY_DAYS)).replace(hour=9, minute=15, second=0, microsecond=0)

        wanted: list[dict[str, Any]] = [i for i in self.instruments if i.get("instrumentType") == "SPOT"]
        for name in {i["name"] for i in self.instruments if i.get("instrumentType") == "FUT"}:
            futures = [i for i in self.instruments if i.get("instrumentType") == "FUT" and i["name"] == name]
            wanted.append(min(futures, key=lambda i: i.get("expiry") or "9999"))

        seeded = 0
        self._pending_series = []
        for item in wanted:
            if self._seed_series(state, item, interval, history_start, now):
                seeded += 1
            else:
                self._pending_series.append(item)
            time.sleep(HISTORY_RATE_DELAY)
        self.series_seeded = True
        return seeded

    def _seed_series(self, state, item, interval, start, end) -> bool:
        token = item.get("historyToken") or item["token"]
        rows = self._fetch_candles(item["exchange"], token, interval, start, end)
        if not rows:
            logger.warning("No history for %s; will retry in the background", item["symbol"])
            return False
        state.seed_candles(item["token"], item, rows)
        logger.info("Seeded %d candles for %s", len(rows), item["symbol"])
        return True

    def retry_series_history(self, state: Any, interval_seconds: int = 60) -> int:
        """Index and futures history must keep retrying; without it the trend gate never opens."""
        if self.smart_api is None or not self._pending_series:
            return 0
        interval = HISTORY_INTERVALS.get(interval_seconds, "ONE_MINUTE")
        now = ist_now()
        session_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now <= session_open:
            return 0
        history_start = (now - timedelta(days=HISTORY_DAYS)).replace(hour=9, minute=15, second=0, microsecond=0)

        recovered = 0
        for item in list(self._pending_series):
            if self._seed_series(state, item, interval, history_start, now):
                self._pending_series.remove(item)
                recovered += 1
            time.sleep(HISTORY_RATE_DELAY)
        return recovered
