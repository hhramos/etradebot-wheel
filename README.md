# ETradeBot — Wheel Strategy UI

A local web UI for running the options wheel strategy against an E\*Trade brokerage account.  
No cloud services. No telemetry. Credentials are held in memory only and never written to disk.

---

## Features

- Live positions with wheel action recommendations (sell CSP, roll, assign, sell CC)
- Options screener with ranked candidates
- Greeks calculator with Ollama AI trade thesis
- 3-year wheel projection with interactive sliders
- Ollama AI advisor for full portfolio analysis
- Demo mode — full UI works without a brokerage account

---

## Quick Start

### Windows

Double-click **`start.bat`** — it finds Python, installs dependencies, starts the server, opens the browser, and auto-launches the Micro Futures Dashboard (port 5001) if the Pro+ plugin is installed.

Or from a terminal:

```cmd
pip install -r requirements.txt
python server.py
```

### macOS / Linux

```bash
pip3 install -r requirements.txt
python3 server.py
```

Or double-click `start.sh` — it installs deps, starts the server, and opens the browser.

Then open **http://127.0.0.1:5000/ui/index.html**.

---

## E\*Trade API Credentials

You need a **developer application** registered at https://developer.etrade.com.

1. Log in and create an app — choose "Individual", enable "Accounts" and "Order" scopes.
2. Copy your **Consumer Key** and **Consumer Secret**.
3. Paste them into the UI each session — they are never saved to disk.

OAuth flow:

1. Paste key + secret → click **Confirm**
2. Click **Launch E\*Trade login** — browser opens E\*Trade's auth page
3. Log in and accept — E\*Trade shows a short **verifier code**
4. Paste the verifier → click **Connect**

Session stays live until you close the tab or restart the server.

---

## Tiers

Tier is detected automatically at startup based on which plugins are present — no configuration required.

| Tier | Requirement | What unlocks |
|------|-------------|--------------|
| **Free** | Base install only | Wheel dashboard, screener, Greeks, AI advisor |
| **Pro** | `ui/greeks.html` present | Greeks Δ buttons per position row, Greeks menu item |
| **Pro+** | `plugins/futures/` present | All Pro features + Futures button (opens port 5001) |

### Installing Pro+ (Micro Futures Dashboard)

Clone the futures plugin into the `plugins/` directory:

```bash
cd path\to\etradebot-wheel
mkdir plugins
cd plugins
git clone https://github.com/hhramos/etradebot-futures.git futures
```

`start.bat` detects the plugin on next launch, installs its dependencies, and starts the futures server automatically on port 5001.

---

## AI Advisor (Ollama)

The wheel advisor and Greeks AI panel require [Ollama](https://ollama.com) running locally.

Recommended models (ranked by quality):

| Model | Quality |
|-------|---------|
| `phi4` | Best |
| `qwen2.5` | Fast and accurate |
| `llama3.1:8b` | Good |
| `mistral:7b` | Good |

```bash
ollama pull qwen2.5
ollama serve
```

The advisor is optional — all other features work without it.

---

## File Layout

```
etradebot-wheel/
├── server.py               ← Entry point (imports srv/ package)
├── requirements.txt
├── start.bat               ← Windows launcher (auto-detects futures plugin)
├── start.sh                ← macOS/Linux launcher
├── srv/                    ← Flask route modules
│   ├── core.py             ← App factory, session, Ollama helpers
│   ├── routes_auth.py      ← OAuth flow, wheel→futures credential bridge
│   ├── routes_account.py   ← Account, positions, orders, market quotes
│   ├── routes_advisor.py   ← Ollama advisor, AI Greeks endpoint
│   ├── routes_screener.py  ← Wheel screener
│   └── routes_tier.py      ← Tier detection
├── ui/
│   ├── index.html          ← Main dashboard
│   ├── greeks.html         ← Greeks calculator (Pro)
│   └── projection.html     ← 3-year projection
├── data/
│   └── config.json         ← Ollama model selection (not credentials)
└── plugins/
    └── futures/            ← Pro+ plugin (git clone etradebot-futures here)
```

---

## Endpoints

### Auth
| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/init` | Store consumer key + secret, get E\*Trade auth URL |
| POST | `/auth/token` | Exchange verifier for access token |
| GET | `/auth/status` | Current connection state |
| POST | `/auth/push_to_futures` | Push credentials to futures server on demand |

### Account & Trading
| Method | Path | Description |
|--------|------|-------------|
| GET | `/account` | Account balances and buying power |
| GET | `/positions` | Open positions with wheel recommendations |
| GET | `/orders` | Open and recently filled orders |
| POST | `/order/preview` | Preview an order (no execution) |
| POST | `/order/submit` | Submit an order to E\*Trade |
| GET | `/market/quote/{ticker}` | Live quote (E\*Trade → yfinance fallback) |

### Screener & Tools
| Method | Path | Description |
|--------|------|-------------|
| GET | `/screener` | Run wheel screener, return ranked candidates |
| GET | `/tier` | Detected tier: free / pro / pro_plus |

### AI Advisor
| Method | Path | Description |
|--------|------|-------------|
| POST | `/advisor/chat` | Stream Ollama reply (SSE) |
| GET | `/advisor/analyze` | Full structured portfolio analysis (SSE) |
| GET | `/advisor/status` | Ollama reachability and model quality |
| POST | `/advisor/model` | Set active Ollama model |
| GET | `/ai/status` | Ollama status for Greeks panel |
| POST | `/ai/greeks` | Generate AI trade thesis from Greeks snapshot |

---

## Demo Mode

If `pyetrade` is not installed **or** you skip the OAuth flow, the server returns realistic demo data for all endpoints. Positions, screener, order cards, Greeks, and the 3-year projection all work without a real brokerage account.

---

## Security

- Consumer key and secret are stored only in the Python process's RAM (`_session` dict) — wiped on server exit
- No credentials are written to disk, logged, or sent anywhere other than E\*Trade's servers
- The server binds to `127.0.0.1` only — not reachable from other machines on the network
- The wheel→futures credential bridge (`/auth/from_wheel` on port 5001) only accepts connections from `127.0.0.1` — external requests are rejected with 403
- The Flask `secret_key` is regenerated with `os.urandom(32)` on every server start

---

## Troubleshooting

**"Server offline" in the UI**  
The Flask server isn't running. Run `python server.py` (Windows) or `python3 server.py` (macOS/Linux).

**Port 5000 already in use**  
`start.bat` clears the port automatically. Manually: end the process in Task Manager (Windows) or `lsof -ti:5000 | xargs kill -9` (macOS/Linux).

**`pyetrade` import errors**  
Run `pip install pyetrade`. The server falls back to demo data if the import fails — only needed for live trading.

**Verifier code rejected**  
Verifier codes expire in ~60 seconds. Complete the OAuth flow promptly after the E\*Trade page loads.

**Futures button opens login page instead of dashboard**  
The wheel pushes credentials to the futures server when you click the Futures button. If the futures server wasn't running when the Wheel completed OAuth, this re-push handles it automatically. If it still shows login, verify the futures server is running on port 5001.

**macOS — `start.sh` won't open**  
Run `chmod +x start.sh` once, then double-click or run `./start.sh`.
