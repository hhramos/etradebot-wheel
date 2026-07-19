# Contributing to ETradeBot

Thank you for your interest in contributing! This guide covers the free/open-source tier (`etradebot`). The Pro and Pro+ tiers are sponsor-only private repos.

## What to Contribute

Good candidates for contributions:
- Bug fixes in `server.py` or `ui/`
- UI improvements to `index.html` or `projection.html`
- Documentation improvements
- Additional E*Trade API integrations (free tier)
- Test coverage in `tests/`

## Getting Started

1. Fork the repo and clone locally
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `etrade_config.example.yaml` to `etrade_config.yaml` and fill in your E*Trade sandbox credentials
4. Run the server: `python server.py` (or `./start.sh` / `start.bat`)
5. Open `http://localhost:5000` and verify the free tier UI loads

## Pull Request Guidelines

- Keep PRs focused — one fix or feature per PR
- Test locally before submitting
- Do not commit credentials, `.db` files, or `__pycache__`
- If your change touches `server.py`, verify the tier detection logic still works for free/Pro/Pro+ tiers
- Add or update tests in `tests/` when fixing bugs

## What Not to Contribute

- Do not open PRs that add features gated behind Pro/Pro+ tiers — those belong in the private repos
- Do not include any E*Trade consumer keys, tokens, or account numbers — even in examples

## Reporting Issues

Open a GitHub Issue with:
- Your OS and Python version
- Steps to reproduce
- The error output (redact any keys or account numbers)

## Code Style

- Python: follow PEP 8, keep functions small
- HTML/JS: match the existing style in `ui/index.html`
- YAML configs: keep comments that explain valid values

## Questions

Use GitHub Discussions for questions about usage or the roadmap.
