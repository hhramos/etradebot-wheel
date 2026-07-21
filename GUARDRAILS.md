# Guardrails — How the Bot Thinks and What It Does

This document explains the strategy behind ETradeBot in plain English. No finance degree required. Think of it as the rulebook the bot follows every single day.

---

## The Big Idea — The Wheel Strategy

The wheel strategy is one of the most predictable ways to generate income from stocks you already like. Here is how it works in plain terms:

**Step 1 — You agree to buy a stock at a discount.**
You sell a "cash-secured put" (CSP). Someone pays you money — called a premium — and in exchange you promise to buy their shares if the stock drops to a price you choose. The stock doesn't have to move at all for you to win. If the stock stays flat or goes up, the option expires worthless and you keep all the premium. Free money.

**Step 2 — If the stock drops and you end up owning it, you put it to work.**
You now sell a "covered call" (CC). You still own the shares but you sell someone else the right to buy them from you at a price slightly above what you paid. They pay you another premium. If the stock rises to that price, they take the shares and you keep both the premium and the profit on the stock. If not, the option expires worthless and you keep the premium again.

**Step 3 — The cycle repeats.**
Every time a position closes, the bot looks for the next opportunity and starts again. This is why it's called the wheel — it keeps spinning, generating premium income as long as you're running it.

The bot handles all three phases automatically. You just say yes or no to new trades.

---

## The Wheel Cycle — Phase by Phase

The bot tracks every position through a series of stages. Here is what each one means:

| Phase | Plain English |
|-------|--------------|
| **IDLE** | No position open. Bot is watching for the right time to enter. |
| **CSP PENDING** | A sell-put order has been placed. Waiting for someone to buy it. |
| **CSP OPEN** | The put is sold and filled. Bot is monitoring it every 30 minutes. |
| **CSP ROLLING** | The put needs to be moved to a later date. Bot is handling it. |
| **ASSIGNED** | The stock dropped below the strike. You now own 100 shares. |
| **CC PENDING** | A covered call order has been placed on those shares. |
| **CC OPEN** | The covered call is sold and filled. Bot is monitoring it. |
| **CC ROLLING** | The covered call is being moved to a later date. |
| **CALLED AWAY** | The shares were sold at the strike price. Cycle complete. |
| **COMPLETE** | The full cycle is done. Profit is logged. Back to IDLE. |

The bot saves this state to a file every time something changes, so it remembers exactly where things stand even if you restart your computer.

![Position monitor — open CSP](screenshots/Wheel%20Position%20monitor%20open%20CSP.png)

---

## The Screener — How the Bot Finds Good Stocks

Before selling any option, the bot runs a screener on a list of 40+ stocks. Think of it as a hiring interview — only the best candidates make the cut.

### Who is on the list?

The default list focuses on companies that have been around a long time, pay dividends, and have liquid options markets. Things like blue-chip banks, consumer staples, healthcare companies, and energy majors. You can add or remove tickers from `data/universe.json`.

### What does it look for?

Every stock gets graded on two scores:

**Fisher Score (0–15 points)**

This measures whether the company is financially solid. The bot checks:

- Is the P/E ratio reasonable? (Not too expensive, not a red flag)
- Is the company growing earnings? (Forward P/E lower than trailing)
- Does the stock pay a dividend? (Dividend yield above 1% gets extra points)
- Is the stock not too wild? (Beta between 0.5 and 1.2 — moves with the market but doesn't swing 20% on a rumor)
- Is debt manageable? (Debt-to-equity below 1.0)
- Is the company profitable on its capital? (Return on equity above 10%)

A Fisher Score of 7 or higher is required to pass. Anything below that doesn't even get considered.

**Wheel Score (0–100 points)**

This measures whether the *options* on that stock are worth selling. The bot checks:

- **Premium yield** — How much premium can you collect as a percentage of the strike? More is better.
- **Implied Volatility (IV)** — Higher IV means fatter premiums. The bot looks for IV above 20% but not so high that the stock is in chaos.
- **Bid/ask spread** — Wide spreads mean the market is thin and you'll lose money just getting in and out. The bot avoids these.
- **Open interest and volume** — Lots of contracts trading means you can get filled at a fair price.

Stocks get graded A, B, C, or D. The bot only considers A and B grades.

### What gets filtered out automatically?

Before a trade is ever placed, the bot removes any stock that:

- Has earnings coming up within 7 days (earnings can blow up a position overnight)
- Has options that are too thinly traded to get a fair fill
- Would require more than 8% of your total account as collateral for one position
- Would push your total deployed capital above 80% of your account

![Screener results](screenshots/Wheel%20Screener%20results.png)


---

## The Guardrails — What the Bot Will Never Do

These are the hard rules. The AI advisor cannot override them. They are baked into the code.

### Position Limits

| Rule | Value | Why |
|------|-------|-----|
| Max open positions at once | 10 | Keeps you diversified without overcomplicating things |
| Max collateral per position | 8% of account | No single stock can hurt you too much |
| Max total capital deployed | 80% of account | Always keep dry powder available |
| Minimum cash reserve | $300 | Never get caught unable to close a trade |

### Option Quality Rules

| Rule | Value | Why |
|------|-------|-----|
| Delta at entry | Between −0.15 and −0.30 | Deep enough to collect meaningful premium, far enough OTM to be safe |
| Days to expiry at entry | 28 to 50 days | The sweet spot for theta decay — time is working for you |
| Earnings blackout — new entries | 7 days before earnings | Earnings announcements move stocks unpredictably |
| Earnings blackout — existing positions | Close 5 days before earnings | Don't hold through an earnings surprise |
| Minimum DTE for new entry | 25 days | Never open a fresh position with less than 25 days left |

### Exit Rules — When the Bot Closes a Trade

| Trigger | What happens |
|---------|-------------|
| **50% profit target** | Bot places a buy-to-close order automatically. This is the most important rule. Taking half your money off the table at 50% decay is proven to improve long-term returns better than holding to expiry. |
| **21 DTE** | If a position is profitable at 21 days to expiry, the bot closes it. Time decay slows down after this point — you've captured most of what you're going to get. |
| **Delta breach** | If the stock drops and the delta hits 1.5× what it was at entry, the bot flags it for a roll. The position hasn't failed — you just need to adjust. |
| **14 DTE at a loss** | If a position is still losing money with only 14 days left, the bot flags it to roll forward. You take a small loss on the old option and sell a new one further out to recover. |

### Roll Rules — How the Bot Handles Trouble

Rolling means closing the current option and opening a new one further out in time, usually for a net credit (meaning they pay you more for the new one than it costs to close the old one).

The bot rolls when:
- Delta has moved too much against you (the stock is heading toward your strike)
- You're running out of time and still at a loss

The bot does **not** roll for the sake of it. If a position can be closed at a profit, it closes it. Rolling is a recovery tool, not a stall tactic.

---

## The Three Modes — How Much the Bot Does on Its Own

| Mode | What happens automatically | What you approve |
|------|---------------------------|-----------------|
| **Dry Run** | Nothing — the bot watches and writes down what it would do | Everything (but nothing actually happens) |
| **Semi** | Closes winning trades at 50% profit automatically | All new entries |
| **Full** | Closes winners, queues new entries based on screener | You can review and cancel anything |

![Mode selector — Dry / Semi / Full](screenshots/Wheel%20Mode%20selector%20dry.semi.full.png)

**Start in Dry Run.** Watch the bot work for a week or two. See what it recommends and why. When you trust it, switch to Semi. When you're comfortable with Semi, switch to Full if you want.

There is no shame in staying on Dry Run. The screener and advisor work just as well in Dry Run — you just do the clicking yourself.

---

## The AI Advisor — Your Second Opinion

The Ollama AI advisor is not the brain of the bot. The guardrails above are the brain. The AI is more like a smart friend you can ask questions.

You can ask it things like:
- "What happened to my SOFI position today?"
- "Should I roll this put or just let it go?"
- "What does the screener think about KO right now?"

It answers in plain English using your real account data. It runs locally on your computer — nothing leaves your machine, and it costs nothing per conversation.

**The AI cannot override the guardrails.** If the rules say no earnings within 7 days, the bot will not enter that trade no matter what the AI says. The rules always win.

![AI Advisor chat](screenshots/Wheel%20AI%20Advisor%20chat.png)


---

## Validating Your Rules — The Backtest Engine

The guardrails above are the defaults. But how do you know the 50% close and 21 DTE rules actually work for *your* stocks?

The backtest engine lets you run your exit rules against real historical data and see the results before committing to them live.

**What to test first:**

Open the Projection page and run the **Compare** mode — it runs 50%-only and 50%+21DTE side by side against the same tickers and the same date range, then shows you the difference in total return, cycles completed, win rate, and average days held.

The 21 DTE rule wins on cycles almost every time, because closing at 21 days frees up capital for the next trade faster. But "almost" isn't proof — run it on your universe and see for yourself.

**What to adjust if you disagree:**

- Think 50% is too conservative? Run the backtest with 60% or 70% as your close target and compare. The KPI table shows you the trade-off.
- Have a custom list of stocks you prefer? Paste them into the "Custom" ticker field — the backtest runs on whatever you give it.

**Ollama analysis (free):** When results are ready, Ollama opens a chat pre-loaded with the full numeric results. Ask it "which tickers dragged the portfolio?" or "would rolling instead of closing have changed the outcome?" — it answers with the real backtest numbers, not generic advice.

The backtest doesn't change any live positions. It's a read-only simulation. Run it as often as you want.

---

## Summary — What the Bot Does While You're Not Watching

Every 30 minutes during market hours, the bot:

1. Checks every open position for the 50% profit target
2. Checks every open position for delta breaches and DTE thresholds
3. Checks for upcoming earnings on any open position
4. Looks for filled orders and updates the wheel phase accordingly
5. If capital is free and you're in Semi or Full mode, runs the screener and looks for a new entry

You get notified if anything needs your attention. You decide what to do with it.

That's the whole system.
