# Reddit Posts — ETradeBot Wheel (Free Version)

---

## r/thetagang

**Title:** Built a local wheel bot that actually enforces the rules most of us argue about — 50% close, 21 DTE, earnings blackout, roll logic. Open source, free.

---

I've been running the wheel for a few years and kept making the same mistakes everyone here argues about: holding past 50% because "it might go higher," forgetting to close at 21 DTE, getting caught holding through earnings.

So I built a bot that enforces the rules by default and won't let me override them without effort.

**What it actually does:**

The core is a wheel state machine — CSP → Assigned → CC → Called Away → repeat. Every 30 minutes during market hours it checks every position against a set of rules:

- **50% profit target**: When a position hits 50% of max profit, it places the buy-to-close automatically. No second-guessing. This is configurable, but 50% is the default because the data is what it is.
- **21 DTE backstop**: If a position is still profitable at 21 days to expiry, it closes regardless. You've collected most of the theta. The last 21 days aren't worth the gamma risk.
- **Delta breach**: If delta drifts to 1.5× the entry delta, the position gets flagged for a roll — not closed, flagged. You still make the call.
- **14 DTE loss rule**: Losing position with 14 days left gets flagged to roll forward for a net credit.
- **Earnings blackout**: No new entries within 7 days of earnings. Existing positions get flagged to close 5 days before. This one has saved me more than anything else.

The roll logic follows a decision tree — net credit available? Roll out same strike. No credit at same strike? Roll out and down. Can't get credit rolling down? Flag for human review. It doesn't roll blindly.

**The screener:**

Before suggesting any trade it runs a two-score filter:

- **Fisher Score (0–15)**: P/E reasonableness, forward vs trailing P/E, dividend yield, beta (0.5–1.2 only), debt-to-equity under 1.0, ROE above 10%. Minimum score of 7 to even be considered.
- **Wheel Score (0–100)**: Premium yield, IV (above 20% but not in chaos territory), bid/ask spread width, open interest, volume. Grades A–D, only A and B get through.

It screens ~40 stocks and surfaces the top 3 candidates with strike, expiry, cash required, and premium in dollars — pre-calculated so morning review takes 10 minutes.

**Three modes:**

- **Dry run**: Watches everything, suggests everything, places nothing. Start here.
- **Semi**: Auto-closes winners at 50%, asks before opening anything new.
- **Full**: Handles exits and queues new entries within your configured rules.

**The backtest — and this is where it gets interesting for thetagang:**

There's a full backtester built in. You pick a date range, starting capital, your ticker universe (full 40, live positions only, or custom list), and then choose which exit rule to test:

- 50% profit close only
- 50% profit OR 21 DTE, whichever comes first
- **Compare both side by side**

That last option is the one I actually use. It runs both rule sets against the same historical data and shows you the KPI difference — total return, number of cycles completed, win rate, average days held. The 21 DTE rule almost always wins on cycles because you free up capital faster, but the comparison makes the case with your own data on your own tickers.

When it's done, Ollama (if you have it running) automatically drops into the results and you can ask it things like:

- *"Which 5 tickers from the universe should I prioritise next 6 months?"*
- *"What difference did the 21-DTE rule make vs 50%-only? How many extra cycles did it enable?"*
- *"What 3 things would you change about my approach based on these results?"*

It answers with the actual backtest numbers in context, not generic advice. The whole thing is free — Ollama runs locally.

Runs locally on your machine. No cloud, no subscription, no API fees.

GitHub: https://github.com/hhramos/etradebot-wheel

Happy to answer questions about the strategy rules or the backtest methodology. The 50% vs 21 DTE comparison was the most useful thing I built — happy to share results.

---

## r/etrade

**Title:** Built an open-source wheel strategy bot on top of E*Trade's API — credentials never leave your machine, no stored tokens, daily re-auth by design

---

I've been using E*Trade for options trading for a while and wanted automation, but every solution I found either required handing credentials to a third-party service or storing API keys in a config file. Neither felt right for a brokerage account.

So I built one that works the way E*Trade's security model actually intends.

**How credentials work:**

You paste your Consumer Key and Consumer Secret into a web form each session. They live in the Python process's RAM — one dictionary in memory — and nowhere else. They are never written to a file, never logged, never sent anywhere except E*Trade's servers during the OAuth handshake. When you close the tab or restart the server, they're gone.

The OAuth flow follows E*Trade's standard 3-leg process:
1. Paste key + secret → server requests a request token from E*Trade
2. E*Trade opens their login page in your browser — you log in directly on their site
3. E*Trade gives you a verifier code → you paste it back → server exchanges it for an access token

That access token lives in memory the same way the credentials do. It auto-expires at midnight ET because that's E*Trade's policy. Every morning you go through the same 20-second flow.

**Why I didn't fight the daily re-auth:**

The daily session expiry is the reason I chose E*Trade for this. A bot with always-on credentials can do real damage before you notice — one edge case, one bad signal, one bug. With E*Trade, the worst case is one day's activity. If anything ever feels off, just don't log in. It stops.

**What the bot actually does:**

It automates the options wheel strategy — selling cash-secured puts, managing assignment, selling covered calls, rolling when needed. Checks positions every 30 minutes during market hours. Has a two-step order flow: preview first (shows you the order with all details), then a separate confirm to place it. You can't accidentally submit an order.

All order activity stays in your E*Trade account history exactly as it would with any approved third-party app — because that's exactly what this is.

**Other security specifics:**

- Server binds to `127.0.0.1` only — not reachable from other machines on your network
- Flask secret key regenerated with `os.urandom(32)` on every server start
- No config file contains credentials — the only config is Ollama model selection (optional AI advisor)
- Demo mode works without any credentials at all — useful for exploring the UI before connecting

It runs locally on Windows, macOS, or Linux. No cloud services, no telemetry, nothing leaves your machine except the E*Trade API calls.

GitHub: https://github.com/hhramos/etradebot-wheel

If you have questions about the OAuth implementation or how the credential bridge works, happy to dig into the details.

---

## r/algotrading

**Title:** Built a local wheel strategy engine on E*Trade's API — Flask + OAuth 1.0a + yfinance + Ollama. Architecture writeup inside.

---

I wanted a wheel automation setup I actually understood and controlled end to end. Here's what I built and the decisions behind it.

**Stack:**

- **Flask** — local web server, `127.0.0.1` only, `os.urandom(32)` secret key per process
- **OAuth 1.0a via `requests-oauthlib`** — E*Trade's auth model, 3-leg flow, tokens in memory only
- **`pyetrade`** — E*Trade REST API wrapper for account, positions, orders, quotes
- **`yfinance`** — fallback quote source and screener data; E*Trade quotes used when session is live
- **`pandas` / `numpy`** — screener scoring and Black-Scholes Greeks calculation
- **Ollama** — local LLM for the advisory layer (optional, zero cost, runs on CPU)
- **`RotatingFileHandler`** — structured logging, 5MB × 3 rotations

**Auth flow:**

```
POST /auth/init   → store key+secret in _session dict, call E*Trade for request token
                  → return auth_url to frontend
GET  [browser]    → user logs in directly on E*Trade's site, gets verifier code
POST /auth/token  → exchange verifier for access token, store oauth object in _session
GET  /auth/status → frontend polls this to know when session is live
```

No token persistence. `_session` is a module-level dict that lives and dies with the Flask process. The E*Trade access token expires at midnight ET regardless.

**Order flow — two-step with no accidental execution:**

```
POST /order/preview  → builds OrderType XML, calls E*Trade preview endpoint
                     → returns preview with filled price, commission, margin impact
POST /order/submit   → takes preview ID from previous response, calls place endpoint
                     → only succeeds if preview ID matches what's in session
```

The preview ID check prevents replay — you can't submit an order that wasn't previewed in this session.

**Screener architecture:**

Two-pass filter on a configurable universe (~40 tickers by default):

1. **Fisher Score** — fundamental filter (P/E, forward vs trailing P/E, dividend yield, beta 0.5–1.2, D/E < 1.0, ROE > 10%). Hard minimum of 7/15.
2. **Wheel Score** — options quality filter (premium yield, IV 20–60% band, bid/ask spread, OI, volume). A/B/C/D grading, A+B only.

Output is a ranked list of candidates with pre-calculated strike, expiry, cash required, and premium.

**Position state machine:**

```
IDLE → CSP_PENDING → CSP_OPEN → CSP_ROLLING → ASSIGNED
ASSIGNED → CC_PENDING → CC_OPEN → CC_ROLLING → CALLED_AWAY → COMPLETE → IDLE
```

State is persisted to `data/wheel_state.json` (gitignored). Each 30-minute heartbeat checks all open positions against exit rules: 50% profit close, 21 DTE close, 1.5× delta breach roll flag, 14 DTE loss roll flag, earnings blackout.

**Ollama integration:**

The advisory endpoint streams from Ollama via SSE. The prompt includes the full account snapshot — positions, unrealized P&L, available buying power, recent fills — so the model has real context. Model is selectable at runtime; defaults to whatever is installed locally. The Greeks panel has a separate endpoint that takes a Black-Scholes snapshot and returns a trade thesis.

**Demo mode:**

If `pyetrade` isn't installed or OAuth is skipped, all endpoints return realistic synthetic data. Useful for testing the UI layer without a live session.

**Backtest engine:**

Built into the projection page. Configurable date range, starting capital, ticker universe (full 40, live positions only, or custom). The interesting part is the exit rule comparison mode — it runs 50%-only and 50%+21DTE in parallel and shows you the KPI diff: total return, cycles completed, win rate, average days held. When results are ready, Ollama picks them up automatically and opens a chat interface with preset prompts: "which tickers outperformed and why," "what would you change," "how many extra cycles did the 21-DTE rule enable." The LLM has the full numeric results as context so it answers with real numbers, not generic advice.

`POST /backtest/run` → runs async, polls `/backtest/status` for progress → `/backtest/context` feeds results to Ollama → `/backtest/analyze` streams the chat response via SSE. Full report available at `/backtest/report`.

**What I deliberately didn't build:**

- No websocket — SSE is sufficient for the update frequency this strategy needs
- No database — `wheel_state.json` and rotating logs are enough for a single-account local tool

The whole thing runs on a laptop. No cloud dependencies, no recurring costs.

GitHub: https://github.com/hhramos/etradebot-wheel

The README has the full endpoint list and file layout. Happy to go deeper on any of the architectural decisions.

---
