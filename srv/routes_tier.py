"""
routes_tier.py — Product tier detection
========================================
Checks the filesystem at startup to determine which product tier is installed.
Detection is hierarchical — highest tier wins:

    plugins/futures/  exists         → pro_plus
    plugins/greeks/   exists         → pro
    futures_executor.py or
      pipeline_orchestrator.py       → pro_plus  (standalone file drop)
    greeks.html or ui/greeks.html    → pro       (standalone file drop)
    None of the above                → free

The /tier endpoint returns the detected tier, feature flags, and display labels.
"""

from pathlib import Path
from flask import jsonify
from srv.core import app

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _detect_tier() -> str:
    """Return 'free', 'pro', or 'pro_plus' based on installed files."""
    base = Path(__file__).resolve().parent.parent  # etradebot/

    # Plugin-style installs
    if (base / "plugins" / "futures").exists():
        return "pro_plus"
    if (base / "plugins" / "greeks").exists():
        return "pro"

    # Standalone file drops
    if (base / "futures_executor.py").exists() or (base / "pipeline_orchestrator.py").exists():
        return "pro_plus"
    if (base / "greeks.html").exists() or (base / "ui" / "greeks.html").exists():
        return "pro"

    return "free"


# Cache at import time so filesystem isn't checked on every request
DETECTED_TIER = _detect_tier()

TIER_LABELS = {
    "free":     "ETradeBot",
    "pro":      "ETradeBot Pro",
    "pro_plus": "ETradeBot Pro+",
}

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.route("/tier", methods=["GET"])
def tier():
    """Return the detected product tier and feature flags."""
    return jsonify({
        "tier": DETECTED_TIER,
        "features": {
            "greeks":  DETECTED_TIER in ("pro", "pro_plus"),
            "futures": DETECTED_TIER == "pro_plus",
        },
        "labels": TIER_LABELS,
    })
