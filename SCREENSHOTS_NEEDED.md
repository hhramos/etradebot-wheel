# Screenshots Needed

Organized by product. Each entry shows the screen to capture, where it goes in the docs, and what the shot needs to demonstrate.

---

## Wheel (etradebot-wheel)

### Setup (SETUP.md)

| # | Screen | Where in Doc | What to Show |
|---|--------|-------------|--------------|
| W-S1 | Login screen — before auth | Step 7 | Consumer Key / Secret fields, "Launch E\*Trade login" button visible |
| W-S2 | E\*Trade OAuth page in browser | Step 7 | The E\*Trade authorization page (blur or crop any account name) |
| W-S3 | Verifier code entry | Step 7 | The short code field + Connect button — reinforces the 60-second warning |
| W-S4 | Dashboard after first login | Step 8 / First Run Checklist | Account balance visible, positions panel, Dry Run badge in header |

### Features / README

| # | Screen | Where in Doc | What to Show |
|---|--------|-------------|--------------|
| W-F1 | Main dashboard overview | README → Features | Full dashboard — balance, positions, screener button, mode badge |
| W-F2 | Screener results | README → How it works | Table of candidates with Fisher Score, Wheel Score, A/B grade highlighted |
| W-F3 | Position monitor — open CSP | GUARDRAILS → Wheel Cycle | A live or paper CSP position showing phase badge (CSP OPEN), DTE, delta |
| W-F4 | Trade suggestion card | GUARDRAILS → Exit Rules | Bot surfacing a 50% profit close suggestion — shows the trigger in action |
| W-F5 | AI Advisor chat | README → AI Advisor | A sample question and response — crop any account-identifying info |
| W-F6 | Mode selector (Dry / Semi / Full) | GUARDRAILS → Three Modes | The mode toggle with Dry Run selected — reassuring to new users |
| W-F7 | 3-year projection page | README → Features | Projection curve with account balance — shows the "why bother" payoff |

---

## Greeks (etradebot-greeks)

### Setup (README → Installation)

| # | Screen | Where in Doc | What to Show |
|---|--------|-------------|--------------|
| G-S1 | Greeks tab appearing in Wheel nav | README → Installation | The toolbar after `greeks.html` is dropped in — "Pro" tier detected |

### Features / README

| # | Screen | Where in Doc | What to Show |
|---|--------|-------------|--------------|
| G-F1 | Greeks panel — single position | README → What's included | Delta / Gamma / Theta / Vega / Rho displayed for a sample CSP |
| G-F2 | P&L heatmap | README → What's included + FAQ | The color grid — price on vertical axis, DTE on horizontal, green/red gradient |
| G-F3 | Strategy builder — iron condor | README → What's included | Multi-leg entry form + combined net Greeks at bottom |
| G-F4 | Theta decay chart | README → Features | The Theta-over-time curve showing the acceleration in final 21 days |
| G-F5 | AI trade thesis output | README → Features | Plain-English "win" and "kill" conditions from Ollama — blur ticker if preferred |

---

## Futures (etradebot-futures)

### Setup (README → Quick Start / First Run Checklist)

| # | Screen | Where in Doc | What to Show |
|---|--------|-------------|--------------|
| F-S1 | First Run / Setup & Status modal | README → First Run Checklist | The checklist modal with steps in pass/fail/waiting state — ideally mid-setup |
| F-S2 | "Init DB" primary button | README → First Run Checklist | Modal showing "Init / Reset DB" button before database exists |
| F-S3 | Dashboard after full setup | README → Quick Start | All checklist items green, environment badge visible (SANDBOX) |

### Features / README + How It Works

| # | Screen | Where in Doc | What to Show |
|---|--------|-------------|--------------|
| F-F1 | Pipeline Signals section | README → How it works | All four signal cards after a pipeline run — prediction, regime, GARCH row |
| F-F2 | GARCH signal rows close-up | README → GARCH / How it works | σ, vol regime badge, stop/TP values — ideally showing "normal" and one "extreme SKIP" |
| F-F3 | Trade Card (Fabio's 8-step checklist) | README → Features | A scorecard with a mix of green/red rows — shows pre-trade gate in action |
| F-F4 | Environment switch modal | README → Security | The SANDBOX → LIVE modal with the red "REAL MONEY" warning visible |
| F-F5 | Positions table | README → Features | Open paper positions with entry price, unrealized P&L, stop price |
| F-F6 | AI Advisor — sanity check response | README → Features | The pre-trade sanity check output including GARCH context line |
| F-F7 | Pipeline run in progress | README → How it works | The "Running…" spinner state — reassures users the bot is working |
| F-F8 | Operations panel | README → Quick Start | Retrain / Preflight / Run Pipeline buttons — shows what "Run Pipeline" means |

---

## General Notes

- **Sandbox is fine** for all screenshots — no real money, no account numbers exposed
- **Blur or crop** any account ID, account number, or email that appears
- **Use paper positions** for any trade/position screenshots — real fills are not needed
- **Consistent window size** — 1440×900 or 1280×800 looks best in GitHub README rendering
- **Light mode preferred** for README screenshots — more legible on GitHub's white background; dark mode variants can go in a `docs/screenshots/` folder for reference
- **File naming convention:** `wheel-dashboard.png`, `greeks-heatmap.png`, `futures-signal-cards.png` etc. — store in `docs/screenshots/` in each repo
