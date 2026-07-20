# Security

## The short version

Your E\*Trade credentials never leave your computer. The bot runs entirely on your own machine and only talks to E\*Trade's official servers. Nobody else — not us, not GitHub, not any cloud service — can see your account.

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

Email instead: *(add your contact email here)*

Please include:
- A description of the issue
- Steps to reproduce it
- What you think the impact could be

We will respond within 3 business days and credit you in the fix if you'd like.

---

## E\*Trade API terms

Using the E\*Trade API for automated trading is explicitly permitted under their developer agreement. You are not violating E\*Trade's terms of service by running this bot. E\*Trade provides the API specifically for applications like this one.

The bot uses OAuth 1.0a — the same authentication flow E\*Trade recommends for all third-party applications.
