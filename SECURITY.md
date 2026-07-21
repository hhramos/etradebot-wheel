# Security

## The short version

Your E\*Trade credentials never leave your computer. The bot runs entirely on your own machine and only talks to E\*Trade's official servers. Nobody else — not us, not GitHub, not any cloud service — can see your account.

---

## Why E\*Trade? Why not a platform with a better API?

This bot could have been built on platforms with more powerful APIs — ones that stay connected around the clock, remember your session forever, and let a bot run completely on its own without any check-in from you.

That was a deliberate choice not to do that.

The thing that looks like E\*Trade's biggest weakness — the fact that your session expires every day and you have to log back in — is actually its most important safety feature. **It means the bot physically cannot do anything without you showing up first.**

Every single day, you have to open the app, type in your credentials, and complete the login. That 20-second routine is not an annoyance. It is your daily confirmation that you are still in control. The bot cannot wake up at 3 AM and start placing orders. It cannot keep running after you've decided to stop. It cannot do anything you haven't personally authorized that morning.

Other platforms let you create API keys that never expire, with permissions wide enough to trade, transfer, and withdraw — all without you touching the keyboard. That is a lot of trust to place in any piece of software, including this one. One bug, one bad signal, one edge case the model never saw — and a bot with persistent credentials can cause real damage before you notice.

With E\*Trade's daily expiry, the worst case is one day's worth of activity. You will know about it the next morning when you log in.

**The re-authentication is not a limitation. It is a circuit breaker built into the platform itself.**

By nature this project takes a fiscally conservative approach. The wheel strategy is already a conservative, income-focused strategy — slow, deliberate, rule-based. The platform matches the philosophy. Once you've been using this for a few weeks and you understand what the bot is doing and why, you'll find you only need to log in once or twice a week anyway. The positions take care of themselves between sessions. The daily login becomes a quick gut-check, not a chore.

If you ever feel like the bot is doing too much or moving too fast, just don't log in. The bot stops. You stay in control.

---

## How your credentials are handled

| What | How it's stored | When it's cleared |
|------|----------------|-------------------|
| Consumer Key & Secret | Memory only — never written to disk | When you close the server |
| OAuth access token | Memory only — never written to disk | When you close the server or click Logout |
| Account balance & positions | Never stored — fetched live on each request | Not stored at all |

There is no database, no credentials file, no `.env` file with your keys. If you restart the server, you start a fresh login. That's intentional.

---

## What the bot can and cannot do

**It can:**
- Read your E\*Trade account balance, positions, and order history
- Place option orders on your behalf (only in `semi` or `full` mode — never in `dry_run`)
- Cancel orders

**It cannot:**
- Withdraw funds or transfer money
- Change your account settings
- Access any account other than the one you authenticate with
- Do anything at all without your E\*Trade API credentials

---

## Network access

The server binds to `127.0.0.1` (your own computer only). It is not reachable from other machines on your network or the internet.

The only outbound connections the bot makes are to:
- `api.etrade.com` / `apisb.etrade.com` — E\*Trade's official REST API
- `finance.yahoo.com` — yfinance fallback for quotes when E\*Trade is unavailable
- `127.0.0.1:11434` — your local Ollama instance (never leaves your machine)

The Pro+ futures bridge (`/auth/from_wheel` on port 5001) only accepts connections from `127.0.0.1`. Any request from a different address gets a 403 immediately.

---

## Files that must never be committed

These are excluded via `.gitignore` — double-check before any `git add .`:

| File | Why |
|------|-----|
| `data/config.json` | Contains your personal strategy settings |
| `data/memory.json` | AI advisor memory with account context |
| `data/trade_log.json` | Your real trade history |
| `data/wheel_state.json` | Current position state |
| `data/guardrails.html` | Your personal trading playbook |

Use `data/config.example.json` as the committed template.

---

## Running safely

1. **Start in `dry_run` mode** (the default). The bot evaluates positions and shows you what it would do — but places zero orders. Watch it for a few days before switching to `semi` or `full`.

2. **Review the source code.** It's small enough to read in an afternoon. Order placement lives in `srv/routes_account.py`. The run-mode guard is the first check before any order is submitted.

3. **Use a dedicated API key.** Create a separate developer application at [developer.etrade.com](https://developer.etrade.com) rather than reusing keys from another project. You can revoke it instantly from the E\*Trade developer portal if needed.

4. **Never share your Consumer Key or Secret** — not in GitHub issues, not in screenshots, not in Discord. Treat them like passwords.

---

## Responsible disclosure

If you find a security vulnerability — especially anything that could expose credentials or allow unintended order placement — please **do not open a public GitHub issue**.

Email instead: hybridworksolutions90@gmail.com

Please include:
- A description of the issue
- Steps to reproduce it
- What you think the impact could be

We will respond within 3 business days and credit you in the fix if you'd like.

---

## E\*Trade API terms

Using the E\*Trade API for automated trading is explicitly permitted under their developer agreement. You are not violating E\*Trade's terms of service by running this bot. E\*Trade provides the API specifically for applications like this one.

The bot uses OAuth 1.0a — the same authentication flow E\*Trade recommends for all third-party applications.
