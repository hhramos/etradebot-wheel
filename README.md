# ETradeBot — Wheel Strategy UI

A local web UI for running the options wheel strategy against an E\*Trade brokerage account.  
No cloud services. No telemetry. Credentials are held in memory only and never written to disk.

---

## Why E\*Trade? Why re-authenticate every day?

This bot could have been built on platforms with more powerful APIs — ones that stay connected forever and let a bot run completely on its own.

That was a choice not to do.

The thing that looks like E\*Trade's biggest weakness — your session expires every day and you have to log back in — is actually its most important safety feature. **The bot cannot do anything without you showing up first.**

Every morning, you open the app and log in. That 20-second routine is your daily confirmation that you are still in control. The bot cannot wake up at 3 AM and start placing orders. It cannot keep running after you've decided to stop. One bug, one bad signal, one edge case the model never saw — and a bot with always-on credentials can cause real damage before you notice. With E\*Trade, the worst case is one day's activity.

Other platforms let you create API keys that never expire, with permissions wide enough to trade, transfer, and withdraw without you ever touching the keyboard. That is a lot of trust to place in any piece of software, including this one.

**The daily re-authentication is not a bug. It is a circuit breaker built into the platform itself.** If you ever feel like the bot is doing too much — just don't log in. It stops. You stay in control.

And in practice, once you have confidence in what the bot is doing, you'll find you only log in once or twice a week anyway. The wheel strategy is slow by design — monthly contracts, 2–5 trades a week. The positions take care of themselves between sessions.

---

## Why does it run on your computer instead of the cloud?

**It's free.** AI services like ChatGPT charge you every time they answer a question. A bot that checks your positions a dozen times a day and runs a screener might make 50–100 of those calls daily — that adds up to $60–200 a month, coming straight out of the money you're trying to earn. ETradeBot uses [Ollama](https://ollama.com), which runs on your own computer. Cost: **$0/month, forever.** A regular laptop with 16GB of RAM handles it just fine.

**Your finances stay in your house.** When you send your account balance, positions, and trades to a cloud AI, that information sits on a server you don't control — possibly used to train models, possibly exposed in a breach. With ETradeBot, none of that leaves your laptop. Your account, your strategy, your history — all of it stays in your living room. Think of the difference between counting your money quietly at the kitchen table versus shouting the numbers in a crowded mall.

**It can't be taken away.** Cloud services raise prices, change their rules, or go down for maintenance right when you need them. Ollama doesn't have a billing department. It doesn't have terms of service. It doesn't go offline at 9:31 AM on a Monday. It just runs.

---

## How it works (plain English)

Think of ETradeBot like a fishing net you set up in the morning.

The old way: you sit by the river all day holding a fishing pole, watching the water, waiting for a bite. If you look away, you miss it. That's trading without automation — staring at screens for hours hoping to catch the right moment.

The ETradeBot way: you spend 10–15 minutes in the morning setting your net, then walk away. The bot watches the river for you. It knows the rules you taught it — which fish to catch, when to pull the net, when to leave it alone.

**What a typical day looks like:**

- **Morning (10–15 min):** Open the app. The bot already screened 40 stocks overnight and ranked them. It shows you three good ones to sell puts on — strike price, cash needed, how much you'd earn — all pre-calculated. You click approve or skip.
- **During the day (0 min):** The bot checks your positions every 30 minutes. If a trade hits 50% profit, it closes it. If something needs attention, it flags it. You don't need to watch.
- **Evening (5 min, optional):** Ask the Wheel Advisor "what happened today?" in plain English. It tells you what it did and why.
- **Once a week (10–15 min):** Run the backtest to see how your strategy performed. Adjust if needed.

![Screener results](screenshots/Wheel%20Screener%20results.png)

The wheel strategy is perfect for this because it's slow by design. You're selling monthly options — 28–50 day contracts — not day trading. Two to five trades a week, not twenty a day. Each trade is pre-calculated, and you just say yes or no.

**The three modes control how much the bot does on its own:**

| Mode | What it does |
|------|-------------|
| **Dry run** | Watches and suggests, but does nothing — training wheels |
| **Semi** | Closes winning trades automatically, asks before opening new ones |
| **Full** | Handles everything within your rules |

---

## Features

- Live positions with wheel action recommendations (sell CSP, roll, assign, sell CC)
- Options screener with ranked candidates
- Greeks calculator with Ollama AI trade thesis
- 3-year wheel projection with interactive sliders
- **Backtest engine** — validate your exit rules against real historical data, with Ollama-powered analysis of the results
- Ollama AI advisor for full portfolio analysis
- Demo mode — full UI works without a brokerage account

![Main dashboard overview](screenshots/Wheel%20Main%20Dashboard%20Overview.png)

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

![AI Advisor chat](screenshots/Wheel%20AI%20Advisor%20chat.png)

![3-year projection page](screenshots/Wheel%20projection%20page.png)

---

## Backtest Engine

The backtest engine lets you run your exit rules against real historical data before you commit to them live. Access it from the **Projection** page.

**What you configure:**

- **Date range** — pick any historical window to test against
- **Starting capital** — the account size to simulate
- **Exit rule** — choose what to test:
  - *50% profit close only*
  - *50% profit OR 21 DTE, whichever comes first*
  - ***Compare both side by side*** — runs both rule sets against the same data and shows you the difference
- **Ticker universe** — all 40 screener stocks, live positions only, or a custom list you type in

**What you get back:**

- KPI grid: total return, number of cycles completed, win rate, average days held
- Portfolio value curve (chart) showing how your capital grew over the period
- Full report link with the complete trade-by-trade log

**The comparison mode is the most useful part.** It runs 50%-only and 50%+21DTE in parallel so you can see — with your own tickers, your own date range — how many extra cycles the 21-DTE rule enabled by freeing up capital faster.

**Ollama analysis (free):** When results are ready, Ollama automatically picks them up and opens a chat with preset prompts:

- *"Which tickers performed best and why?"*
- *"Which positions lost money?"*
- *"Which 5 tickers should I prioritize next 6 months?"*
- *"What difference did the 21-DTE rule make vs 50%-only? How many extra cycles?"*
- *"What 3 things would you change about my approach?"*

The AI answers with the actual backtest numbers in context — not generic advice.

**Save state:** A checkbox lets you seed the live bot from your backtest's final positions, so you can pick up where the simulation ended.

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

---

## Frequently asked questions

**Is this safe? Can it drain my account?**  
No. The bot defaults to `dry_run` mode — it watches your positions and makes suggestions but places zero orders. Even in `semi` or `full` mode, it can only place option orders. It cannot withdraw money, transfer funds, or touch your cash. See [SECURITY.md](SECURITY.md) for the full breakdown.

**Do I need coding experience?**  
You need to be comfortable installing Python and running a command in a terminal. You do not need to read or write any code to use it.

**Does it work on Mac and Linux?**  
Yes. Run `start.sh` or `python3 server.py`. Everything works the same as Windows.

**Do I need Ollama / the AI advisor?**  
No. The screener, position monitor, order panel, projection page, and backtest engine all work without it. Ollama adds the AI chat, trade thesis, and backtest analysis features — all free when running locally.

**What Ollama model should I use?**  
`qwen2.5` for speed, `phi4` for the best analysis. Both run fine on a laptop with 16GB RAM.

**Will E\*Trade ban me for using a bot?**  
No. E\*Trade provides the developer API specifically so you can build tools like this. The bot uses their official OAuth flow — the same one any approved third-party app uses.

**Do I need a special E\*Trade account?**  
A standard E\*Trade brokerage account with options trading enabled. Apply for API access at [developer.etrade.com](https://developer.etrade.com) — it's free.

**Why do I have to log in again every morning?**  
E\*Trade OAuth tokens expire daily. This is an E\*Trade policy, not something the bot controls. `start.bat` / `start.sh` includes a one-click re-auth flow — it takes about 20 seconds.

**What's the difference between the three modes?**  
`dry_run` — watches and suggests, places no orders (start here).  
`semi` — automatically closes winning trades at 50% profit, asks before opening new ones.  
`full` — handles exits and queues new entries, all within your configured rules.

**What's the difference between the three modes?**  
`dry_run` — watches and suggests, places no orders (start here).  
`semi` — automatically closes winning trades at 50% profit, asks before opening new ones.  
`full` — handles exits and queues new entries, all within your configured rules.

**What is the Pro / Pro+ tier?**  
The free version covers the full wheel strategy — screener, position monitor, advisor, projection, and backtest engine. Pro adds the Greeks analyzer. Pro+ adds the Micro Futures ML pipeline (MES, MNQ, MYM, M2K). See the Tiers section above.

**Something broke — how do I report it?**  
Open an issue on GitHub using the [bug report template](https://github.com/hhramos/etradebot-wheel/issues/new?template=bug_report.md). The template asks for your OS, Python version, and any error text — that's usually all we need.

**How do I suggest a new feature?**  
Use the [feature request template](https://github.com/hhramos/etradebot-wheel/issues/new?template=feature_request.md). Plain English is fine — no need to write code or specs.

**The screener returned zero results — what's wrong?**  
Three common reasons: (1) Markets are closed — the screener needs live data. (2) Your filters are too strict — try relaxing the Fisher Score minimum or the IV threshold. (3) yfinance had a temporary hiccup — wait a minute and try again.

**My E\*Trade login works but the bot says "not connected" — why?**  
This usually means the verifier code timed out (they expire in about 60 seconds). Click the E\*Trade login button again and complete the flow more quickly. If it keeps happening, try a different browser — some browser extensions can interfere with the redirect.

**Can I use this with a Roth IRA or retirement account?**  
Yes, as long as your account has options trading enabled. Cash-secured puts and covered calls are generally permitted in IRAs. Check with E\*Trade if you're unsure whether your specific account type allows it.

**How much money do I need to start?**  
That depends on the stocks you want to trade. A cash-secured put on a $50 stock costs $5,000 in collateral (100 shares × $50). The screener focuses on liquid stocks with affordable prices, but you'll want at least $5,000–$10,000 free cash to have meaningful choices. Paper trade first regardless of account size.

**Will it work if my computer goes to sleep?**  
The bot stops monitoring when your computer sleeps or the server process stops. It picks up where it left off when you restart it — nothing is lost, but it won't have checked positions while it was off. This is intentional: you stay in control.
