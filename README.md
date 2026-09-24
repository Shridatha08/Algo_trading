# Option Signal Workbench

A starter workspace for AngelOne SmartAPI market data and live option-trading signals.

## Architecture

- `python-service/`: AngelOne login, SmartAPI WebSocket V2 ticks, OI snapshots, candle aggregation, and strategy hook.
- `node-dashboard/`: Node.js server that serves the dashboard and relays Python state over WebSocket.
- No order placement is implemented. This project is market-data and signal-display only until the strategy is defined and validated.

## Prerequisites

- Python 3.10+
- Node.js 20+
- AngelOne SmartAPI credentials: API key, client code, PIN, and TOTP secret

## Setup

1. Copy `.env.example` to `.env` and fill in the credentials and instrument settings. Never commit `.env`.
2. Create and activate a Python virtual environment:

   ```bash
   cd python-service
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Install and run the Node dashboard in a second terminal:

   ```bash
   cd node-dashboard
   npm install
   npm run dev
   ```

4. Run the Python service in the first terminal:

   ```bash
   cd python-service
   source .venv/bin/activate
   python -m app.main
   ```

5. Open http://localhost:3000.

### Verify SmartAPI before streaming

With the Python service running, test only the AngelOne login (no WebSocket subscription and no orders):

```bash
curl -X POST http://127.0.0.1:8000/api/angel/verify
```

Success returns `{"status":"ok","authenticated":true,"feedTokenReceived":true}`. An error usually means the API key, client code, PIN, TOTP secret, device clock, or SmartAPI account access needs attention. TOTP secrets must be entered as the base32 secret from AngelOne, not the six-digit code currently displayed in an authenticator.

The Python service starts in demo-safe mode if credentials are missing, so the dashboard can be developed without exposing secrets. Set `DATA_MODE=live` only after verifying credentials and instrument tokens.

## Configuration

- `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET`: SmartAPI login values.
- `DATA_MODE`: `demo` or `live`.
- `SYMBOL`, `EXCHANGE`, `TOKEN`: instrument subscribed to SmartAPI WebSocket V2. `EXCHANGE` accepts `NSE`, `NFO`, `BSE`, `BFO`, `MCX`, `NCDEX`, or `CDS`.
- `SUBSCRIBE_ALL_OPTIONS=true`: discovers instruments from the AngelOne scrip master.
- `OPTION_UNDERLYINGS=NIFTY`: index underlyings to scan. Add `,BANKNIFTY` to widen.
- `STOCK_UNDERLYINGS`: comma-separated stock F&O underlyings, for example `RELIANCE,TCS,HDFCBANK,INFY`. Empty by default.
- `STRIKE_WINDOW=45`: strikes either side of ATM to subscribe per underlying. NIFTY's full front expiry is ~91 strikes, so this covers the whole chain.
- `MAX_SUBSCRIPTIONS=500`: token budget. SmartAPI allows 1000 per WebSocket session.
- `SCRIP_MASTER_URL`: official AngelOne instrument-master URL.

### Subscription budget

Spot, India VIX and the front-month future are subscribed first, then strikes are windowed around each underlying's live spot. Far-dated futures are dropped since only the front month is used. NIFTY alone uses 185 of 500 tokens with the full front-expiry chain; 2 indices plus 8 stocks at `STRIKE_WINDOW=15` uses 607 of 1000.

Volatility regime uses India VIX for indices and the contract's own ATM implied volatility for stocks, since VIX does not describe single-stock volatility.

## Signal engine

All three strategies run on the 5-minute series that carries volume (index futures, or the cash series for stocks), and every setup is option buying only.

1. **9/15 EMA momentum scalp** — EMA9 above/below EMA15 with a separation filter of `MIN_TREND_SEPARATION` x ATR and a candle pushing the trend. Stop at EMA15 or the 5-candle swing.
2. **ORB + Supertrend** — break of the first `ORB_CANDLES` candles' range, confirmed by Supertrend direction. Stop at the opposite range edge or the Supertrend line. Valid until `ORB_VALID_UNTIL`.
3. **VWAP mean-reversion bounce** — price stretched `VWAP_STRETCH_ATR` x ATR from VWAP with a reversal candle closing back toward it. Stop at the reversal candle extreme.
4. **EMA 20/50 trend + VWAP + OI** — the slower trend filter: price aligned with EMA20 over EMA50, on the correct side of VWAP, with open interest not opposing. Stop at the 20-candle swing.

Every strategy is evaluated on each cycle and priced into a full trade plan; the best risk-reward is selected. `STRATEGY_PRIORITY` only sets display order and breaks ties. The dashboard lists each strategy as selected, qualified, rejected, blocked or not triggered.

Gates: sufficient candles (`MIN_CANDLES`), live expiry, OI populated, session window, and `MIN_RISK_REWARD` at T2. Buying is skipped when the volatility regime is `high`.

Tunables:

- `RISK_FREE_RATE` (default `0.065`), `MIN_RISK_REWARD` (default `1.2`) for debit spreads.
- `MIN_CREDIT_FRACTION` (default `0.25`): minimum credit as a fraction of spread width.
- `PREMIUM_SL_FRACTION` (default `0.35`), `SESSION_START`/`SESSION_END` (default `09:20`/`15:15` IST).
- `VIX_LOW`, `VIX_ELEVATED`, `VIX_HIGH` (defaults `12`, `18`, `25`).

The engine is analytical only. It places no orders, and its output must be backtested and paper-traded before any capital is committed.
- `CANDLE_INTERVAL_SECONDS`: candle bucket size, default `60`.
- `PYTHON_SERVICE_URL`: Node-to-Python URL, default `http://127.0.0.1:8000`.

## Next step

Define the signal algorithm precisely: underlying and option contracts, timeframe, entry conditions, stop-loss formula, target 1/2/3 formulas, position sizing, and whether signals are long-only or both directions. The implementation should be backtested and paper-traded before any order execution is added.
