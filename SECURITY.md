# Security Policy

## Sensitive Files

**Never commit these files** — they contain credentials or personal data:

- `data/config.json` — contains your E\*Trade API keys and trading rules
- `data/trade_log.json` — your trade history
- `data/memory.json` — AI advisor conversation memory
- `data/wheel_state.json` — current wheel position state

These are excluded via `.gitignore`. Use `data/config.example.json` as a template.

## API Keys

ETradeBot uses E\*Trade OAuth. Your consumer key and secret should only exist in `data/config.json` (gitignored) or environment variables. Never paste them into code files.

## Ollama

The AI advisor runs locally via Ollama. No data is sent to external AI services. All inference happens on your machine.

## Reporting Vulnerabilities

If you find a security issue, please open a private issue or contact the maintainer directly rather than posting publicly.
