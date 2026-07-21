# Initial Setup Guide

Getting ETradeBot running for the first time takes about 20–30 minutes. You only do this once. After that, starting the bot every day takes about 20 seconds.

This guide assumes you have never done this before. Take it one step at a time.

---

## Do You Need an E\*Trade Account?

**Not to get started.** The screener, backtest engine, Greeks calculator, 3-year projection, and AI advisor all work without one. They pull live and historical market data from yfinance — no brokerage login required.

You only need an E\*Trade account when you're ready to connect your real portfolio and start placing trades. Most people run the screener and backtest for a few weeks first to build confidence in the strategy before connecting.

If you just want to explore, skip straight to Step 1 and Step 2 — you'll be running in under 5 minutes.

---

## What You Need Before You Start

- A computer running Windows, macOS, or Linux
- About 30 minutes and a cup of coffee *(an E\*Trade account only needed for live trading)*

That's it. Everything else gets installed in the steps below.

---

## Step 1 — Install Python

Python is the language the bot is written in. Think of it as the engine under the hood.

### Windows

1. Go to [python.org/downloads](https://www.python.org/downloads/)
2. Click the big yellow **Download Python** button
3. Run the installer
4. **Important:** On the first screen, check the box that says **"Add Python to PATH"** before clicking Install. If you miss this, the bot won't be able to find Python.
5. Click **Install Now**

To check it worked, open a Command Prompt (search for `cmd` in the Start menu) and type:

```
python --version
```

You should see something like `Python 3.12.4`. Any version 3.10 or higher is fine.

### macOS

macOS may already have Python, but it's often an old version. Install a fresh one:

1. Go to [python.org/downloads](https://www.python.org/downloads/)
2. Download and run the macOS installer
3. Open Terminal (search for it in Spotlight) and type:

```bash
python3 --version
```

You should see `Python 3.10` or higher.

### Linux

```bash
sudo apt update && sudo apt install python3 python3-pip
```

---

## Step 2 — Download ETradeBot

If you have Git installed:

```bash
git clone https://github.com/hhramos/etradebot-wheel.git
cd etradebot-wheel
```

If you don't have Git, go to the GitHub page, click the green **Code** button, and choose **Download ZIP**. Unzip it somewhere you'll remember, like your Desktop or Documents folder.

---

## Step 3 — Install the Bot's Dependencies

The bot relies on several helper libraries. This command installs all of them in the right order.

Open a terminal (or Command Prompt on Windows), navigate to the etradebot-wheel folder, and run:

```bash
pip install -r requirements.txt
```

On macOS or Linux, you may need `pip3` instead:

```bash
pip3 install -r requirements.txt
```

This installs the following, in dependency order:

| Library | What it does | Why it's needed |
|---------|-------------|-----------------|
| `requests` | Makes web requests | Everything that talks to the internet uses this |
| `requests-oauthlib` | Handles OAuth login | The secure handshake with E\*Trade's servers |
| `flask` | Runs the local web server | Powers the dashboard you see in your browser |
| `flask-cors` | Lets the browser talk to the server | Required for the dashboard to function |
| `pyetrade` | Speaks E\*Trade's API language | Places orders, reads positions, fetches quotes |
| `yfinance` | Pulls stock data from Yahoo Finance | Powers the screener and quote fallback |
| `pandas` | Organizes data into tables | Used by the screener to rank and filter stocks |
| `numpy` | Does the math | Powers the Black-Scholes calculations in the Greeks tool |
| `pytest` | Runs automated tests | Used to verify the bot works correctly (optional for daily use) |

The whole install takes about 2–3 minutes depending on your internet speed.

**If you see any red error messages**, the most common fix is:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Step 4 — Install Ollama (Optional but Recommended)

Ollama runs the AI advisor on your own computer. It's free and nothing leaves your machine.

1. Go to [ollama.com](https://ollama.com) and download the installer for your system
2. Install it and let it run in the background
3. Open a terminal and pull a model:

```bash
ollama pull qwen2.5
```

This downloads about 4–5 GB. You only do it once.

If you have a newer laptop with more RAM, `phi4` gives better analysis:

```bash
ollama pull phi4
```

**You can skip this step entirely** and come back to it later. The screener, position monitor, and order tools all work without Ollama. Only the AI chat and trade thesis need it.

---

## Step 5 — Get Your E\*Trade API Keys

1. Log in to your E\*Trade account
2. Go to [developer.etrade.com](https://developer.etrade.com)
3. Create a new application — choose **Individual**, enable **Accounts** and **Order** scopes
4. Copy your **Consumer Key** and **Consumer Secret** — you'll paste these into the bot each session

These are not stored anywhere. You paste them in when you start the bot each morning.

---

## Step 6 — Start the Bot

### Windows (easiest)

Double-click **`start.bat`** in the etradebot-wheel folder.

It will:
- Find Python automatically
- Install any missing dependencies
- Start the server
- Open your browser to the dashboard

### macOS / Linux

```bash
./start.sh
```

Or if that doesn't work, run:

```bash
chmod +x start.sh
./start.sh
```

### Manual start (any system)

```bash
python server.py
```

Then open your browser and go to: **http://127.0.0.1:5000/ui/index.html**

---

## Step 7 — Log In to E\*Trade

1. The dashboard opens with a login screen

![Login screen before auth](screenshots/Wheel%20Login%20Screen%20Before%20Auth.png)

2. Paste your Consumer Key and Consumer Secret
3. Click **Confirm**
4. Click **Launch E\*Trade login** — your browser opens E\*Trade's website

![E*Trade OAuth page](screenshots/Wheel%20Etrade%20OAuth%20Page.png)

5. Log in to E\*Trade and click Authorize
6. E\*Trade shows you a short **verifier code** (5–6 characters)
7. Copy that code, paste it back in the bot, and click **Connect**

![Verifier code entry](screenshots/Wheel%20Verifier%20code%20entry.png)

You're in. The dashboard now shows your real account balance and positions.

**The verifier code expires in about 60 seconds.** Don't walk away during this step.

---

## Step 8 — First Run Checklist

Before letting the bot do anything, take 5 minutes to do this:

- [ ] Set your run mode to **Dry Run** in the dashboard (it should default to this)
- [ ] Check the positions panel — does it show your real E\*Trade positions correctly?
- [ ] Run the screener — does it return a list of candidates?
- [ ] Ask the AI advisor a simple question like "What do you see in my portfolio?" (only if Ollama is running)
- [ ] Look at the projection page — does your account balance appear correctly?

If all of that works, you're good to go.

![Dashboard after first login](screenshots/Wheel%20Dashboard%20after%20first%20login.png)

---

## Everyday Startup (After First Time)

Once you've done the initial setup, every morning looks like this:

1. Double-click `start.bat` (Windows) or run `./start.sh` (Mac/Linux) — **10 seconds**
2. Paste your Consumer Key and Secret, click Confirm — **5 seconds**
3. Complete the E\*Trade login and paste the verifier code — **20 seconds**
4. You're in — **30 seconds total**

That's it. The bot picks up exactly where it left off.

---

## If Something Goes Wrong

**"python is not recognized"** (Windows)  
You forgot to check "Add Python to PATH" during install. Re-run the Python installer and check that box.

**"pip is not recognized"** (Windows)  
Try `python -m pip install -r requirements.txt` instead.

**"No module named flask"**  
The dependencies didn't install. Run `pip install -r requirements.txt` again.

**"pyetrade not installed"**  
Run `pip install pyetrade`. The bot will work in demo mode without it — you just can't connect to a live E\*Trade account.

**The verifier code doesn't work**  
They expire fast. Click the E\*Trade login button again and complete the process more quickly.

**The browser shows a blank page**  
Make sure the server is actually running. Look for the terminal window — it should say `Running on http://127.0.0.1:5000`.

**Ollama isn't responding**  
Open a terminal and run `ollama serve`. Then try the AI advisor again.

---

## You're All Set

If you made it through all eight steps, ETradeBot is running on your computer, connected to your E\*Trade account, and ready to help you run the wheel strategy.

Start in Dry Run. Watch it for a week. Ask it questions. When you're comfortable with what it's doing and why, switch to Semi mode.

See [GUARDRAILS.md](GUARDRAILS.md) to understand the strategy and rules the bot follows.  
See [SECURITY.md](SECURITY.md) to understand how your credentials are protected.
