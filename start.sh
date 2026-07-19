#!/bin/bash
set -e

echo ""
echo "  ============================================"
echo "   ETradeBot — Wheel Strategy UI"
echo "  ============================================"
echo ""

# Move to repo root (one level up from ui/)
cd "$(dirname "$0")"

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "  ERROR: python3 not found. Install from python.org"
    exit 1
fi

# Install dependencies
echo "  Installing dependencies..."
python3 -m pip install flask flask-cors pyetrade yfinance pandas numpy --quiet

# Kill any existing server on 5000
lsof -ti:5000 | xargs kill -9 2>/dev/null || true

# Start server in background
echo "  Starting server..."
python3 server.py &
SERVER_PID=$!

# Wait for server
sleep 1.5

# Open browser
URL="http://127.0.0.1:5000/ui/index.html"
echo "  Opening $URL"
if command -v open &>/dev/null; then
    open "$URL"          # macOS
elif command -v xdg-open &>/dev/null; then
    xdg-open "$URL"      # Linux
fi

echo ""
echo "  Server PID: $SERVER_PID"
echo "  Press Ctrl+C to stop"
echo ""

wait $SERVER_PID
