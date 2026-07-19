# ETradeBot — Wheel Strategy UI

A local web UI for running the options wheel strategy against an E-Trade brokerage account.  
No cloud services. No telemetry. Credentials are held in memory only and never written to disk.

---

## Quick start

### macOS / Linux

```bash
pip3 install -r requirements.txt
python3 server.py
```

Then open **http://127.0.0.1:5000/ui/index.html** in your browser.  
Or just double-click `start.sh` — it installs deps, starts the server, and opens the browser automatically.

### Windows

Double-click `start.bat`.  
Or from a terminal:

```cmd
pip install -r requirements.txt
python server.py
```

---

## File layout

```
etradebot/
├── server.py           ← Flask backend (all API endpoints)
├── requirements.txt    ← Python dependencies
├── start.sh            ← macOS/Linux one-click launcher
├── start.bat           ← Windows one-click launcher
├── README.md           ← This file
└── ui/
    ├── index.html      ← Main dashboard (positions, screener, order cards)
    └── projection.html ← 3-year wheel projection with interactive sliders
```

---

## E-Trade API credentials

You need a **developer application** registered at https://developer.etrade.com.

1. Log in and create an app — choose "Individual" and enable the "Accounts" and "Order" scopes.
2. Copy your **Consumer Key** and **Consumer Secret**.
3. Paste them into the UI each session (they are never saved).

The OAuth flow is entirely browser-based:

1. Paste key + secret → click **Confirm**
2. Click **Launch E-Trade login** — your browser opens E-Trade's auth page
3. Log in and accept — E-Trade shows a short **verifier code**
4. Paste the verifier into the UI → click **Connect**

Done. The session stays live until you close the tab or restart the server.

---

## Demo mode

If `pyetrade` is not installed **or** you skip the OAuth flow, the server returns
realistic demo data for all endpoints. Everything in the UI works — positions,
screener, order cards, and the 3-year projection — without a real brokerage account.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/init` | Store consumer key + secret, get E-Trade auth URL |
| POST | `/auth/token` | Exchange verifier for access token |
| GET | `/auth/status` | Current connection state |
| GET | `/account` | Account balances and buying power |
| GET | `/positions` | Open positions with wheel action recommendations |
| GET | `/orders` | Open and recently filled orders |
| POST | `/order/preview` | Preview an order (no execution) |
| POST | `/order/submit` | Submit an order to E-Trade |
| GET | `/screener` | Run wheel screener, return ranked candidates |
| POST | `/run` | Run `bot.run()` in preview or live mode |

---

## Connecting the real wheel screener

If you have the full `etradebot` repo checked out alongside this folder, the
`/screener` endpoint will automatically import and run
`strategies.wheel_screener_strategy.WheelScreenerStrategy`. Otherwise it falls
back to built-in demo candidates.

Place this `ui/` folder and `server.py` inside the repo root so Python can find
the `strategies/` module:

```
etradebot-repo/
├── bot.py
├── strategies/
│   └── wheel_screener_strategy.py
├── server.py          ← this file
└── ui/
    ├── index.html
    └── projection.html
```

---

## Security notes

- Consumer key and secret are stored only in the Python process's RAM (`_session` dict).  
  They are wiped when the server process exits.
- No credentials are written to disk, logged, or sent anywhere other than E-Trade's servers.
- The server binds to `127.0.0.1` only — it is not reachable from other machines on your network.
- The Flask `secret_key` is regenerated with `os.urandom(32)` on every server start.

---

## Troubleshooting

**"Server offline" in the UI**  
The Flask server isn't running. Run `python3 server.py` in your terminal.

**Port 5000 already in use**  
Kill whatever's on it: `lsof -ti:5000 | xargs kill -9` (macOS/Linux) or find and end the process in Task Manager (Windows). Then restart.

**`pyetrade` import errors**  
Run `pip3 install pyetrade`. The server falls back to demo data gracefully if the import fails, so this is only needed for live trading.

**Verifier code rejected**  
Verifier codes expire quickly. Complete the flow within ~60 seconds of the E-Trade page loading.

**macOS — `start.sh` won't open**  
Run `chmod +x start.sh` once, then double-click or run `./start.sh`.
