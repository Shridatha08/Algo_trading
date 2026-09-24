from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

logger = logging.getLogger(__name__)

EXCHANGE_TYPES = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "NCDEX": 7, "CDS": 13}
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "INDIA VIX"}
INDEX_FALLBACK_TOKENS = {"NIFTY": "26000", "BANKNIFTY": "26009", "INDIA VIX": "26017"}


def _cache_dir() -> Path:
    default = Path(__file__).resolve().parent.parent / ".cache"
    return Path(os.getenv("SCRIP_CACHE_DIR", default))


def fetch_scrip_master(url: str) -> list[dict[str, Any]]:
    """The master changes once a day, so reuse today's copy instead of re-downloading ~40MB."""
    cache_file = _cache_dir() / f"scrip-master-{date.today().isoformat()}.json"
    if cache_file.exists():
        logger.info("Using cached scrip master %s", cache_file.name)
        return json.loads(cache_file.read_text())

    logger.info("Downloading AngelOne scrip master (this takes a while on first run today)")
    with urlopen(url, timeout=180) as response:
        instruments = json.load(response)

    try:
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(instruments))
        temporary.replace(cache_file)
        for stale in cache_dir.glob("scrip-master-*.json"):
            if stale != cache_file:
                stale.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Could not cache scrip master: %s", error)

    logger.info("Scrip master loaded: %d records", len(instruments))
    return instruments


def load_market_universe(
    url: str, underlyings: list[str], stock_underlyings: list[str] | None = None
) -> list[dict[str, Any]]:
    """Load index spot, India VIX, futures, and options for index and stock underlyings."""
    instruments = fetch_scrip_master(url)

    wanted = {value.strip().upper() for value in underlyings if value.strip()}
    stocks = {value.strip().upper() for value in (stock_underlyings or []) if value.strip()}
    today = date.today()
    selected: list[dict[str, Any]] = []
    index_candidates: dict[str, list[str]] = {}
    seen_equity: set[str] = set()

    for instrument in instruments:
        segment = instrument.get("exch_seg")
        name = str(instrument.get("name", "")).upper()
        symbol = str(instrument.get("symbol", "")).upper()
        kind = instrument.get("instrumenttype")

        if segment == "NSE" and (name in INDEX_SYMBOLS or symbol in INDEX_SYMBOLS):
            label = name if name in INDEX_SYMBOLS else symbol
            if label == "INDIA VIX" or label in wanted:
                index_candidates.setdefault(label, []).append(str(instrument["token"]))
            continue

        # Stock spot is the cash-market -EQ scrip and, unlike indices, needs no alias token.
        if segment == "NSE" and name in stocks and symbol == f"{name}-EQ" and name not in seen_equity:
            seen_equity.add(name)
            selected.append(
                {
                    "token": str(instrument["token"]),
                    "symbol": name,
                    "name": name,
                    "instrumentType": "SPOT",
                    "assetClass": "STOCK",
                    "exchange": "NSE",
                    "exchangeType": EXCHANGE_TYPES["NSE"],
                }
            )
            continue

        if segment != "NFO":
            continue
        is_index = name in wanted and kind in ("OPTIDX", "FUTIDX")
        is_stock = name in stocks and kind in ("OPTSTK", "FUTSTK")
        if not (is_index or is_stock):
            continue
        expiry = _parse_expiry(instrument.get("expiry"))
        if expiry is None or expiry < today:
            continue
        option = kind in ("OPTIDX", "OPTSTK")
        selected.append(
            {
                "token": str(instrument["token"]),
                "symbol": instrument.get("symbol", ""),
                "name": name,
                "instrumentType": "OPT" if option else "FUT",
                "assetClass": "INDEX" if is_index else "STOCK",
                "exchange": "NFO",
                "exchangeType": EXCHANGE_TYPES["NFO"],
                "expiry": expiry.isoformat(),
                "strike": _parse_strike(instrument.get("strike")) if option else None,
                "optionType": str(instrument.get("symbol", ""))[-2:] if option else None,
                "lotSize": _parse_int(instrument.get("lotsize")),
            }
        )

    for label in set(index_candidates) | {key for key in INDEX_FALLBACK_TOKENS if key == "INDIA VIX" or key in wanted}:
        tokens = index_candidates.get(label, [])
        preferred = INDEX_FALLBACK_TOKENS.get(label)
        # The master lists each index twice; pin the documented streaming token instead of file order.
        token = preferred if preferred in tokens else (tokens[0] if tokens else preferred)
        # getCandleData rejects the streaming index token and needs the AMXIDX variant.
        history_token = next((value for value in tokens if value.startswith("999")), token)
        selected.append(
            {
                "token": token,
                "historyToken": history_token,
                "symbol": label,
                "name": label,
                "instrumentType": "VIX" if label == "INDIA VIX" else "SPOT",
                "assetClass": "INDEX",
                "exchange": "NSE",
                "exchangeType": EXCHANGE_TYPES["NSE"],
            }
        )

    return selected


def _parse_expiry(value: Any) -> date | None:
    if not value:
        return None
    for pattern in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(value), pattern).date()
        except ValueError:
            continue
    return None


def _parse_strike(value: Any) -> float | None:
    try:
        return round(float(value) / 100, 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None