from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from threading import Thread

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .angel import AngelMarketData
from .config import get_settings
from .state import MarketState

logging.basicConfig(level=logging.INFO)
DEMO_MINUTES = int(os.getenv("DEMO_MINUTES", "300"))
settings = get_settings()
state = MarketState(settings.candle_interval_seconds)
angel: AngelMarketData | None = None


def seed_demo_tick() -> None:
    """Synthetic market so the dashboard is developable without live credentials."""
    os.environ.setdefault("IGNORE_SESSION_WINDOW", "true")
    base = datetime.now(timezone.utc) - timedelta(minutes=DEMO_MINUTES)
    expiry = (date.today() + timedelta(days=7)).isoformat()

    for name, spot_token, start_price, step, lot in (
        ("NIFTY", "26000", 24000.0, 50, 75),
        ("BANKNIFTY", "26009", 52000.0, 100, 15),
    ):
        spot = start_price
        for minute in range(DEMO_MINUTES):
            stamp = (base + timedelta(minutes=minute)).isoformat()
            spot += step * 0.12 if minute % 4 else -step * 0.08
            state.add_tick({"token": spot_token, "symbol": name, "underlying": name,
                            "instrumentType": "SPOT", "ltp": round(spot, 2), "volume": 0, "timestamp": stamp})
            state.add_tick({"token": f"FUT{name}", "symbol": f"{name}FUT", "underlying": name,
                            "instrumentType": "FUT", "expiry": "2030-01-30", "ltp": round(spot + step * 0.4, 2),
                            "volume": 1000 * (minute + 1), "oi": 900000, "timestamp": stamp})

        atm = round(spot / step) * step
        for offset in range(-8, 9):
            strike = atm + offset * step
            for kind in ("CE", "PE"):
                moneyness = (strike - spot) if kind == "CE" else (spot - strike)
                premium = max(4.0, step * 2.4 - 0.35 * moneyness)
                state.add_tick({
                    "token": f"{name}{kind}{strike}", "symbol": f"{name}{strike}{kind}", "underlying": name,
                    "instrumentType": "OPT", "expiry": expiry, "strike": float(strike), "optionType": kind,
                    "lotSize": lot, "ltp": round(premium, 2),
                    "oi": int(200000 + 90000 * (1 if abs(offset) in (4, 6) else 0)),
                    "oiChange": -12000 if (kind == "CE" and offset == 4) else 8000,
                })

    state.add_tick({"token": "99926017", "symbol": "INDIA VIX", "underlying": "INDIA VIX",
                    "instrumentType": "VIX", "ltp": 13.4})
    state.set_status("demo")


def _background_history(client: AngelMarketData) -> None:
    while True:
        try:
            if not client.series_seeded:
                client.seed_history(state, settings.candle_interval_seconds)
            client.retry_series_history(state, settings.candle_interval_seconds)
            client.seed_watched_options(state, settings.candle_interval_seconds)
        except Exception as error:
            logging.getLogger(__name__).warning("History refresh failed: %s", error)
        time.sleep(20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global angel
    if settings.data_mode.lower() == "live":
        try:
            angel = AngelMarketData(settings, state.add_tick)
            angel.start()
            state.set_status("live")
            # Backfill in the background so the API is available while history downloads.
            Thread(target=_background_history, args=(angel,), daemon=True).start()
        except Exception as error:
            state.set_status("error", str(error))
    else:
        seed_demo_tick()
    yield


app = FastAPI(title="Option Signal Market Data", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "dataMode": settings.data_mode}


@app.post("/api/angel/verify")
def verify_angel_credentials() -> dict:
    """Test SmartAPI login without subscribing to ticks or placing orders."""
    client = AngelMarketData(settings, state.add_tick)
    try:
        result = client.verify_credentials()
        return {"status": "ok", **result}
    except Exception as error:
        return {"status": "error", "message": str(error)}


@app.get("/api/state")
def get_state() -> dict:
    return state.snapshot(settings.symbol, settings.exchange)


@app.get("/api/positions")
def get_positions() -> dict:
    state.snapshot(settings.symbol, settings.exchange)
    return state.paper_snapshot()


@app.post("/api/positions/{position_id}/close")
def close_position(position_id: int) -> dict:
    return state.close_paper_position(position_id)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(state.snapshot(settings.symbol, settings.exchange))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.port, reload=False)
