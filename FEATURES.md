# ETradeBot — Feature List
> Wheel strategy automation for E*Trade. Open-source, local, no cloud, no subscription.

---

## Core Bot

### Autonomous Monitoring
- Scheduler fires automatically on NYSE market hours (pre-market, open, 6× intraday, pre-close, post-close)
- Three run modes: **dry_run** (log only, zero orders), **semi** (auto-execute BTC exits only), **full** (auto-execute exits + queue entries)
- Mode toggle in topbar with confirmation dialog — color-coded amber/blue/green pill
- Reinvestment log on projection page shows every queued/executed action in real time

### Wheel Strategy Engine
- Full 8-phase wheel guardrail playbook enforced on every decision
- 50% profit target detection fires every 30 minutes during market hours
- 21 DTE backstop — closes positions automatically if profit target not hit
- Earnings blackout: no new entries within 7 days of earnings
- Roll decision tree: ITM CSPs automatically evaluated for roll vs close
- Position sizing by NAV tier — max 5% collateral per position enforced
- Sector concentration cap — prevents over-allocation to single sector

### E*Trade Integration
- Full OAuth 1.0a authentication flow with one-click re-auth
- Preview→place two-step order submission (prevents E*Trade error 101)
- Live option chain fetch for roll panel (real bid/ask, actual OI)
- GTC and DAY order support
- Order cancellation from UI
- After-hours price display with `◷` indicator

---

## Screener

### Candidate Scoring
- **Fisher score** (0–15): fundamental quality via P/E, dividend yield, earnings growth, market cap
- **Wheel grade** (A–D) and score (0–100): composite of IV, Fisher, dividend yield, liquidity
- **IV Rank** (0–100): where current IV sits in its 52-week range — primary entry timing signal
- **Black-Scholes delta** at CSP strike: target -0.15 to -0.30 shown color-coded
- **SMA trend**: price vs 50d and 200d moving averages — warns against entries below 200d SMA
- **Post-earnings badge**: flags 1-3 days post-earnings as IV crush opportunity window
- **VIX regime banner**: 5-scenario guidance (crisis / favorable / moderate / normal / compressed) with hover tooltips explaining strategy implications per scenario

### Signal Integration
- Insider trading signals (SEC Form 4, last 30 days) — links to EDGAR filings
- Congressional trading signals (Capitol Trades, last 30 days) — links to filings
- Social sentiment (StockTwits bullish %, last 2 weeks)
- All signals loadable on demand with "Load signals" button

### Screener Table
- 15 columns, 9 sortable (Price, P/E, Div%, IV%, IVR, Δ, Fisher, Wheel, Coll%)
- Hover tooltips on every column header explaining the metric and wheel strategy relevance
- Intraday timing warning on order card (9:30–10:00 AM ET spread caution)
- "over 5%" badge on positions exceeding NAV cap
- Filters: sector, min Fisher, min wheel grade, hide >5% positions

![Screener results](screenshots/Wheel%20Screener%20results.png)


---

## Order Management

### CSP Order Cards
- Top 3 picks shown as tabbed order cards, auto-populated from screener
- Full order details: symbol, strike, expiry, contracts, limit price, total credit, breakeven, cash required, return on cash
- Contracts slider (1–10) with live recalculation of credit and cash required
- Send to E*Trade button with preview→place confirmation
- BTC (buy-to-close) card unlocks after CSP fill confirmation
- GTC BTC order pre-calculated at 50% of opening premium
- P&L progress bar tracking toward 50% target

![Trade suggestion card](screenshots/Wheel%20Trade%20suggestion%20card.png)


### Custom Entry Card
- Type any ticker → live lookup → instant order card
- Fisher score, Wheel grade, IV%, dividend yield shown inline
- Same order card flow as screener picks

### Roll Panel
- Live option chain loaded from E*Trade API
- Multi-leg roll: BTC existing + STO new in one submission
- Strike/expiry selection with real bid/ask prices
- Net credit/debit calculation per roll

---

## Wheel Advisor (AI)

### Local Ollama Integration
- Runs entirely on your machine — no API costs, no data leaves your device
- Supports phi4, qwen2.5, and other Ollama models
- Model quality classifier (phi4/qwen2.5 = "good" tier; smaller models flagged)
- Streaming responses with stop button and Escape key abort
- Response time display per model

### Advisor Features
- Full 8-phase guardrail playbook injected as system context
- Quick Decision Tree for every entry/exit routing decision
- 30-day memory: NAV history, closed cycles, per-ticker win/loss, last 3 AI recommendations
- Position context: all open positions with P&L, DTE, action recommendation
- Screener context: top candidates with IV rank, delta, trend
- Full Analysis mode: deep context including backtest results and memory
- 6 quick-question buttons (today's actions, this week, this month, why these actions, explain opportunity, alternative actions)
- Entry timing rules: IV Rank thresholds, VIX regime, SMA trend, post-earnings window baked into every recommendation

![AI Advisor chat](screenshots/Wheel%20AI%20Advisor%20chat.png)


---

## Positions Table

### Position Display
- Live underlying price with after-hours indicator
- Initial premium (sold) column for CSP/CC positions
- Total gain ($ and %) for stock positions
- Market value for monitor-only stock holdings (shares × current price)
- Fisher score and Wheel grade per position
- Action recommendation: BTC Ready / Hold / Sell CC / Roll / Close+Recycle / Monitor
- Urgency color coding: green (action profitable) / amber (hold) / red (urgent) / blue (new entry)

### Summary Bar
- Open positions count, P&L today, total collateral, pending actions count, open orders count

---

## Projection & Backtest

###  Projection
- Three scenarios: A (base, flat capital), B (reinvest income), C (reinvest + margin)
- Adjustable inputs: starting capital, monthly yield, margin level, margin APR, tax drag, bad months per year
- Portfolio value chart (36 months, Chart.js)
- Monthly income table for all three scenarios
- 6-month order calendar with planned STO/BTC/CC entries
- Risk summary for each scenario
- Live data refresh from connected account

### Backtest Engine
- 1-year historical backtest using yfinance data
- Black-Scholes premium estimation at each entry
- Full cycle tracking: CSP open → 50% BTC or 21 DTE → assignment → CC → called away
- Per-ticker win rate, average P&L, roll count
- Worst/best 5 trades surfaced for Ollama analysis
- HTML report matching ETradeBot dark UI
- `--save-state` flag: exports `wheel_state.json` so live bot picks up where backtest ended
- Ollama backtest analysis: rich context (individual cycle records, per-ticker stats) fed to local AI

![3-year projection page](screenshots/Wheel%20projection%20page.png)


---

## Greeks Analyzer (Pro tier — `greeks.html`)

- Standalone page, opens in new tab, works offline (no server needed)
- Full Black-Scholes engine computed client-side
- 5 Greek cards: Δ Γ Θ V ρ with per-dollar impact and color-coded progress bars
- 6 model input sliders: stock price, strike, DTE, IV%, risk-free rate, contracts
- P&L heatmap: stock price (±25%) × DTE grid, hover tooltips
- 4 real-time Chart.js charts: delta profile, theta decay, P&L at expiry, vega × IV
- Multi-leg strategy builder: spreads, strangles, custom structures
- Position import from live positions table via postMessage
- 9-scenario P&L table with current spot highlighted
- Greek reference panel with wheel-strategy context
- "Send to Greeks" button on every order card and screener row

---

## Infrastructure & Security

- Consumer key and secret held in **memory only** — never written to disk, cleared on tab close
- No credentials logged — access tokens stored in Flask session memory only
- Rotating server log (`data/server.log`, 2MB, 3 backups) with in-UI download
- Log export: last 100/500/1000/all lines, dated filename, security warning displayed before download
- Demo fallback on every endpoint — full UI testable without E*Trade connection
- Cross-platform: `start.bat` (Windows) and `start.sh` (Mac/Linux)

---

## Hardware & Cost

- Runs on any Python 3.10+ machine
- Tested: ThinkStation PGX GB10, ThinkPad X13s (Snapdragon ARM64), standard Windows/Mac
- Ollama advisor cost: $0 (local inference)
- Optional Claude API upgrade: ~$3.70/month for Sonnet 4.6 quality
- E*Trade API: free (apply at developer.etrade.com)

---

## Roadmap / Pro Tier

- `greeks.html` Greeks analyzer
- Enhanced Ollama advisor prompts and context templates
- Persistent credentials (Fernet encryption)
- Email/SMS alerts on 50% target hit or ITM alert
- Multi-account support
