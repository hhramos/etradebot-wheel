# Screenshots

Organized by product. ✅ = captured and embedded · ⬜ = still needed.

All Wheel screenshots live in `screenshots/` on `main`.

---

## Wheel (etradebot-wheel)

### Setup (SETUP.md)

**W-S1 ✅ — Login screen before auth** *(SETUP.md → Step 7)*

![Login screen before auth](screenshots/Wheel%20Login%20Screen%20Before%20Auth.png)

---

**W-S2 ✅ — E\*Trade OAuth page in browser** *(SETUP.md → Step 7)*

![E*Trade OAuth page](screenshots/Wheel%20Etrade%20OAuth%20Page.png)

---

**W-S3 ✅ — Verifier code entry** *(SETUP.md → Step 7)*

![Verifier code entry](screenshots/Wheel%20Verifier%20code%20entry.png)

---

**W-S4 ✅ — Dashboard after first login** *(SETUP.md → Step 8 / First Run Checklist)*

![Dashboard after first login](screenshots/Wheel%20Dashboard%20after%20first%20login.png)

---

### Features / README

**W-F1 ✅ — Main dashboard overview** *(README → Features)*

![Main dashboard overview](screenshots/Wheel%20Main%20Dashboard%20Overview.png)

---

**W-F2 ✅ — Screener results** *(README → How it works)*

![Screener results](screenshots/Wheel%20Screener%20results.png)

---

**W-F3 ✅ — Position monitor — open CSP** *(GUARDRAILS.md → Wheel Cycle)*

![Position monitor open CSP](screenshots/Wheel%20Position%20monitor%20open%20CSP.png)

---

**W-F4 ✅ — Trade suggestion card** *(GUARDRAILS.md → Exit Rules)*

![Trade suggestion card](screenshots/Wheel%20Trade%20suggestion%20card.png)

---

**W-F5 ✅ — AI Advisor chat** *(README → AI Advisor)*

![AI Advisor chat](screenshots/Wheel%20AI%20Advisor%20chat.png)

---

**W-F6 ✅ — Mode selector (Dry / Semi / Full)** *(GUARDRAILS.md → Three Modes)*

![Mode selector](screenshots/Wheel%20Mode%20selector%20dry.semi.full.png)

---

**W-F7 ✅ — 3-year projection page** *(README → Features)*

![Projection page](screenshots/Wheel%20projection%20page.png)

---

## Greeks (etradebot-greeks)

### Setup (README → Installation)

**G-S1 ✅ — Greeks tab appearing in Wheel nav**
*README → Installation — toolbar after `greeks.html` is dropped in, "Pro" tier detected*

![Greeks tab appearing in Wheel nav](screenshots/Greeks%20Greeks%20tab%20appearing%20in%20Wheel%20.png)

---

### Features / README

**G-F1 ✅ — Greeks panel — single position**
*README → What's included — Delta / Gamma / Theta / Vega / Rho for a sample CSP*

![Greeks panel — single position](screenshots/Greeks%20Greeks%20Panel%20.png)

---

**G-F2 ✅ — P&L heatmap**
*README → What's included + FAQ — color grid, price vertical, DTE horizontal*

![P&L heatmap](screenshots/Greeks%20P%26L%20heatmap.png)

---

**G-F3 ✅ — Strategy builder — iron condor**
*README → What's included — multi-leg entry form + combined net Greeks*

![Strategy builder](screenshots/Greeks%20Strategy%20Builder.png)

---

**G-F4 ✅ — Theta decay chart**
*README → Features — Theta-over-time curve, acceleration in final 21 days*

![Theta decay chart](screenshots/Greeks%20Theta%20decay%20chart.png)

---

**G-F5 ✅ — AI trade thesis output**
*README → Features — plain-English win/kill conditions from Ollama*

![AI trade thesis output](screenshots/Greeks%20AI%20trade%20thesis%20output.png)

---

## Futures (etradebot-futures)

### Setup (README → Quick Start / First Run Checklist)

**F-S1 ✅ — First Run / Setup & Status modal**
*README → First Run Checklist — checklist modal mid-setup, mix of pass/fail/waiting*

![First Run / Setup & Status modal](screenshots/Futures%20First%20Run%20Setup.png)

---

**F-S2 ✅ — "Init DB" primary button state**
*README → First Run Checklist — modal before database exists, button reads "Init / Reset DB"*

![Init DB button state](screenshots/Futures%20Init%20DB.png)

---

**F-S3 ✅ — Dashboard after full setup**
*README → Quick Start — all checklist items green, SANDBOX badge visible*

![Dashboard after full setup](screenshots/Futures%20Complete%20Setup.png)

---

### Features / README + How It Works

**F-F1 ✅ — Pipeline Signals section**
*README → How it works — all signal cards after a pipeline run, prediction + regime + GARCH rows*

![Pipeline Signals section](screenshots/Futures%20Pipeline%20Signals.png)

---

**F-F2 ✅ — GARCH signal rows close-up**
*README → GARCH / How it works — σ, vol regime badge, stop/TP*

![GARCH signal rows close-up](screenshots/Futures%20GARCH%20signal%20rows%20close-up.png)

---

**F-F3 ✅ — Trade Card (8-step checklist)**
*README → Features — scorecard with mix of green/red rows*

![Trade Card — 8-step checklist](screenshots/Futures%20Trade%20Card%20checklist.png)

---

**F-F4 ✅ — Environment switch modal**
*README → Security — SANDBOX → LIVE modal with red "REAL MONEY" warning*

![Environment switch modal](screenshots/Futures%20Environmental%20switch%20modal%20.png)

---

**F-F5 ⬜ — Positions table**
*README → Features — open paper positions, entry price, unrealized P&L, stop price*

---

**F-F6 ✅ — AI Advisor sanity check response**
*README → Features — pre-trade sanity check output including GARCH context line*

![AI Advisor sanity check response](screenshots/Futures%20AI%20Advisor%20sanity%20check%20response.png)

---

**F-F7 ✅ — Pipeline run in progress**
*README → How it works — the "Running…" spinner state*

![Pipeline run in progress](screenshots/Futures%20Pipeline%20run%20in%20progress.png)

---

**F-F8 ✅ — Operations panel**
*README → Quick Start — Retrain / Preflight / Run Pipeline buttons*

![Operations panel](screenshots/Futures%20Operations%20Panel.png)

---

### Additional / Extra

**F-F9 ✅ — Candlestick charts**

![Charts](screenshots/Futures%20Charts.png)

---

**F-F10 ✅ — Charts magnified**

![Charts magnified](screenshots/Futures%20Charts%20Magnified.png)

---

**F-F11 ✅ — Run Pipeline Now button**

![Run Pipeline Now](screenshots/Futures%20Run%20Pipeline%20Now.png)

---

## Progress

| Product | Captured | Needed | Total |
|---------|----------|--------|-------|
| Wheel   | 11 ✅    | 0 ⬜   | 11    |
| Greeks  | 6 ✅     | 0 ⬜   | 6     |
| Futures | 13 ✅    | 1 ⬜   | 14    |
| **All** | **30**   | **1**  | **31**|
