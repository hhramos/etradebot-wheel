"""
ETradeBot — entrypoint.

server.py was a 3,248-line monolith; it is now a package:

    srv/core.py             shared state, config, helpers, scheduler (order-sensitive)
    srv/routes_auth.py      OAuth flow
    srv/routes_account.py   account / positions / orders reads
    srv/routes_orders.py    order preview / submit / chain / roll
    srv/routes_screener.py  screener + bot run
    srv/routes_advisor.py   Ollama advisor (SSE)
    srv/routes_botctl.py    bot control, backtest, data & log export
    srv/routes_tier.py      product tier detection (free/pro/pro_plus)

Import order below is deliberate: core executes all module-level init
(config, guardrails, ADVISOR_SYSTEM resolution, scheduler thread) exactly
as the monolith did, then route modules attach to the same `app`.
"""
from srv.core import app, logger          # noqa: F401  (executes all init)
import srv.routes_auth                    # noqa: F401,E402
import srv.routes_account                 # noqa: F401,E402
import srv.routes_orders                  # noqa: F401,E402
import srv.routes_screener                # noqa: F401,E402
import srv.routes_advisor                 # noqa: F401,E402
import srv.routes_botctl                  # noqa: F401,E402
import srv.routes_tier                    # noqa: F401,E402

if __name__ == "__main__":
    from srv.routes_tier import DETECTED_TIER, TIER_LABELS
    print(f"\n  {TIER_LABELS[DETECTED_TIER]} server starting... (tier: {DETECTED_TIER})")
    print("  Open: http://localhost:5000/ui/index.html\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
