"""Bot control & data: /bot/*, /backtest/*, /data/*, /log/* — extracted verbatim from server.py."""
from srv.core import (
    SNAPSHOTS_DIR, TRADE_LOG, _BASE_DIR, _CFG_PATH, _LogCapture, _append_trade_log,
    _backtest_state, _et_now_str, _json, _ollama_reachable, _pending_actions, _pending_lock,
    _prepare_cc_order, _session, _stream_ollama, app, json, jsonify,
    logger, os, request, threading,
)


@app.route("/bot/projection_data", methods=["GET"])
def bot_projection_data():
    """
    Return all data the projection page needs in one call:
    account, positions, screener top-10, wheel_state stats,
    reinvestment log (last 10), universe tiers.
    """
    import os, json as _json

    # Account
    acct = {}
    try:
        import pyetrade
        api = pyetrade.ETradeAccounts(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        acct = api.get_account_balance(_session["account_id"], account_type="MARGIN")
    except Exception:
        acct = {
            "net_value":     _session.get("_net_value", 6568),
            "buying_power":  435,
            "cash_balance":  130,
        }

    # Positions (use cached)
    positions = _session.get("_last_positions", [])

    # Screener (use cached, top 10) — cache format: {"candidates": [...], "by_ticker": {...}}
    screener_cache = _session.get("_screener_cache", {})
    if isinstance(screener_cache, dict) and "candidates" in screener_cache:
        _candidates_list = screener_cache.get("candidates", [])
    elif isinstance(screener_cache, dict):
        _candidates_list = list(screener_cache.values())
    else:
        _candidates_list = []
    screener = sorted(
        [c for c in _candidates_list if isinstance(c, dict)],
        key=lambda c: (-c.get("wheel_score", 0), -c.get("fisher_score", 0))
    )[:10]

    # Wheel state stats
    wheel_stats = {"active_wheels": 0, "lifetime_premium": 0, "cycles_completed": 0}
    state_path  = os.path.join(_BASE_DIR, "data", "wheel_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                ws = _json.load(f)
            wheels = ws.get("wheels", {})
            active = sum(1 for w in wheels.values()
                        if w.get("phase") not in ("IDLE", "COMPLETE", None))
            wheel_stats = {
                "active_wheels":      active,
                "lifetime_premium":   ws.get("total_premium_collected", 0),
                "cycles_completed":   ws.get("total_cycles_completed", 0),
                "wheels":             {
                    t: {"phase": w.get("phase"), "ticker": t,
                        "strike": w.get("csp_strike") or w.get("cc_strike"),
                        "expiry": w.get("csp_expiry") or w.get("cc_expiry"),
                        "premium": w.get("csp_premium") or w.get("cc_premium"),
                        "total_premium": w.get("total_premium", 0),
                        "roll_count": w.get("roll_count", 0),
                        "cost_basis": w.get("cost_basis"),
                        "contracts": w.get("csp_contracts") or w.get("cc_contracts") or
                                     (w.get("shares_held", 0) // 100),
                    }
                    for t, w in wheels.items()
                }
            }
        except Exception as e:
            logger.warning("projection wheel_state read: %s", e)

    # Reinvestment log (last 10 REINVEST_DECISION events)
    reinvest_log = []
    log_path = os.path.join(_BASE_DIR, "data", "trade_log.json")
    if os.path.exists(log_path):
        try:
            with open(log_path) as f:
                all_log = _json.load(f)
            reinvest_log = [
                e for e in reversed(all_log)
                if e.get("event") in (
                    "REINVEST_DECISION", "CYCLE_COMPLETE",
                    "ASSIGNED",          "CALLED_AWAY",
                    "CC_ORDER_PREPARED", "CC_ORDER_SUBMITTED",
                    "ROLL_EXECUTED",     "POLL",
                    "DAILY_SUMMARY",
                    "QUEUED_EXIT",       "QUEUED_ENTRY",   # wheel_bot queued actions
                    "EXIT_PLACED",       "CSP_FILLED",     # wheel_bot executions
                )
            ][:15]
        except Exception:
            pass

    # Universe tiers
    universe = {}
    uni_path = os.path.join(_BASE_DIR, "data", "universe.json")
    if os.path.exists(uni_path):
        try:
            with open(uni_path) as f:
                uni = _json.load(f)
            tier_map = uni.get("tier_map", {})
            tiers = {"micro": [], "small": [], "mid": [], "large": [], "other": []}
            for ticker in uni.get("tickers", []):
                t = tier_map.get(ticker, "other")
                tiers.setdefault(t, []).append(ticker)
            universe = tiers
        except Exception:
            pass

    return jsonify({
        "account":       acct,
        "positions":     positions,
        "screener":      screener,
        "wheel_state":   wheel_stats,
        "reinvest_log":  reinvest_log,
        "universe_tiers": universe,
    })


@app.route("/data/log", methods=["GET"])
def data_log():
    """Return last N entries from trade_log.json."""
    n = min(int(request.args.get("n", 50)), 500)
    try:
        if not os.path.exists(TRADE_LOG):
            return jsonify({"entries": [], "total": 0})
        with open(TRADE_LOG, "r") as f:
            entries = _json.load(f)
        return jsonify({"entries": entries[-n:], "total": len(entries)})
    except Exception as e:
        return jsonify({"error": str(e), "entries": []})


@app.route("/data/summary/<date_str>", methods=["GET"])
def data_summary(date_str):
    """Return daily summary for a given date (YYYY-MM-DD)."""
    path = os.path.join(SNAPSHOTS_DIR, f"{date_str}_summary.json")
    if not os.path.exists(path):
        return jsonify({"error": "No summary for this date"}), 404
    with open(path, "r") as f:
        return jsonify(_json.load(f))


@app.route("/log/info", methods=["GET"])
def log_info():
    """Return metadata about the server log file."""
    try:
        _lp = os.path.join(_BASE_DIR, "data", "server.log")
        st   = os.stat(_lp)
        with open(_lp, "r", encoding="utf-8", errors="replace") as _f:
            lines = sum(1 for _ in _f)
        # Last non-empty line for timestamp preview
        with open(_lp, "r", encoding="utf-8", errors="replace") as _f:
            last = ""
            for line in _f:
                if line.strip():
                    last = line.strip()
        last_ts = last[:19] if len(last) >= 19 else last
        return jsonify({
            "size_kb":    round(st.st_size / 1024, 1),
            "lines":      lines,
            "last_entry": last_ts,
            "exists":     True,
        })
    except FileNotFoundError:
        return jsonify({"exists": False, "size_kb": 0, "lines": 0, "last_entry": "—"})
    except Exception as e:
        return jsonify({"exists": False, "error": str(e)})


@app.route("/log/export", methods=["GET"])
def log_export():
    """Return tail of server log as a downloadable text file."""
    from flask import Response as _Resp
    import datetime as _dt
    lines_n = min(int(request.args.get("lines", 500)), 50000)
    try:
        _lp = os.path.join(_BASE_DIR, "data", "server.log")
        with open(_lp, "r", encoding="utf-8", errors="replace") as _f:
            all_lines = _f.readlines()
        tail = "".join(all_lines[-lines_n:]) if lines_n > 0 else "".join(all_lines)
        fname = f"etradebot_log_{_dt.date.today()}.txt"
        return _Resp(
            tail,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={fname}"}
        )
    except FileNotFoundError:
        return _Resp(
            "No log file found — the server may have just started.\n"
            "Logs are written to data/server.log.\n",
            mimetype="text/plain",
        )
    except Exception as e:
        return _Resp(f"Log export error: {e}\n", mimetype="text/plain")


_CFG_EXAMPLE_PATH = os.path.join(_BASE_DIR, "data", "config.example.json")


def _load_or_init_config() -> dict:
    """Load config.json, creating it from config.example.json if missing."""
    if not os.path.exists(_CFG_PATH):
        os.makedirs(os.path.dirname(_CFG_PATH), exist_ok=True)
        try:
            with open(_CFG_EXAMPLE_PATH) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        cfg.setdefault("run_mode", "dry_run")
        with open(_CFG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        logger.info(f"Created data/config.json from config.example.json")
    with open(_CFG_PATH) as f:
        return json.load(f)


@app.route("/bot/mode", methods=["GET"])
def get_bot_mode():
    """Return current run_mode from config.json."""
    try:
        cfg = _load_or_init_config()
        return jsonify({"mode": cfg.get("run_mode", "dry_run")})
    except Exception as e:
        logger.error(f"get_bot_mode: config read failed: {e}")
        return jsonify({"mode": "dry_run", "error": str(e)})


@app.route("/bot/mode", methods=["POST"])
def set_bot_mode():
    """Change run_mode in config.json — takes effect on next scheduler cycle."""
    data = request.get_json() or {}
    mode = data.get("mode", "").strip()
    if mode not in ("dry_run", "semi", "full"):
        return jsonify({"error": f"invalid mode '{mode}' — must be dry_run, semi, or full"}), 400
    try:
        cfg = _load_or_init_config()
        old_mode = cfg.get("run_mode", "dry_run")
        cfg["run_mode"] = mode
        with open(_CFG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        logger.info(f"[BOT] run_mode changed: {old_mode} → {mode}")
        return jsonify({"success": True, "mode": mode, "previous": old_mode})
    except Exception as e:
        logger.error(f"set_bot_mode error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/bot/pending", methods=["GET"])
def bot_pending():
    """Return all pending CC orders awaiting approval."""
    with _pending_lock:
        pending = [a for a in _pending_actions if a["status"] == "pending"]
    return jsonify({"pending": pending, "count": len(pending)})


@app.route("/bot/pending/<action_id>/approve", methods=["POST"])
def bot_pending_approve(action_id):
    """Approve a pending CC order and submit it to E*Trade."""
    with _pending_lock:
        action = next((a for a in _pending_actions if a["id"] == action_id), None)
    if not action:
        return jsonify({"error": "Action not found"}), 404
    if action["status"] != "pending":
        return jsonify({"error": f"Action already {action['status']}"}), 400

    # Allow override of limit price from request body
    data        = request.get_json() or {}
    limit_price = float(data.get("limit_price", action["limit_price"]))
    contracts   = int(data.get("contracts",   action["contracts"]))

    try:
        import pyetrade
        api = pyetrade.ETradeOrder(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<PlaceOrderRequest>
  <orderType>OPTN</orderType>
  <clientOrderId>CC{abs(hash(action_id)) % 10000000}</clientOrderId>
  <Order>
    <allOrNone>false</allOrNone>
    <priceType>LIMIT</priceType>
    <orderTerm>{"GOOD_UNTIL_CANCEL" if action.get("tif","DAY").upper()=="GTC" else "GOOD_FOR_DAY"}</orderTerm>
    <marketSession>REGULAR</marketSession>
    <limitPrice>{limit_price}</limitPrice>
    <Instrument>
      <Product>
        <securityType>OPTN</securityType>
        <symbol>{action["ticker"].upper()}</symbol>
        <callPut>CALL</callPut>
        <expiryYear>{action["expiry"][:4]}</expiryYear>
        <expiryMonth>{action["expiry"][5:7]}</expiryMonth>
        <expiryDay>{action["expiry"][8:10]}</expiryDay>
        <strikePrice>{action["strike"]}</strikePrice>
      </Product>
      <orderAction>SELL_OPEN</orderAction>
      <quantityType>QUANTITY</quantityType>
      <quantity>{contracts}</quantity>
    </Instrument>
  </Order>
</PlaceOrderRequest>"""
        result = api.place_equity_order(
            resp_format="json", account_id=_session["account_id"],
            order_xml=body
        )
        order_id = (result.get("PlaceOrderResponse", {})
                         .get("OrderIds", {}).get("orderId", "?"))
        with _pending_lock:
            action["status"]      = "submitted"
            action["order_id"]    = str(order_id)
            action["submitted_et"]= _et_now_str()
            action["limit_price"] = limit_price
        _append_trade_log({
            "ts": _et_now_str(), "event": "CC_ORDER_SUBMITTED",
            "ticker": action["ticker"], "order_id": str(order_id), "action": action,
        })
        logger.info(f"[CC] Submitted: {action['ticker']} ${action['strike']}C "
                    f"{action['expiry']} ×{contracts}c @ ${limit_price} → #{order_id}")
        return jsonify({"success": True, "order_id": str(order_id), "action": action})
    except Exception as e:
        logger.error(f"[CC] Submit failed: {e}")
        # Surface the failure — a fake success here would leave the user
        # believing a covered call is live while the position is unhedged.
        with _pending_lock:
            action["status"] = "failed"
            action["error"]  = str(e)[:200]
        return jsonify({"success": False, "error": str(e),
                        "detail": "CC order was NOT placed. Position remains "
                                  "unhedged — retry from the pending panel.",
                        "action": action}), 502


@app.route("/bot/pending/<action_id>/reject", methods=["POST"])
def bot_pending_reject(action_id):
    """Reject/dismiss a pending action."""
    data   = request.get_json() or {}
    reason = data.get("reason", "Manually dismissed")
    with _pending_lock:
        action = next((a for a in _pending_actions if a["id"] == action_id), None)
    if not action:
        return jsonify({"error": "Not found"}), 404
    action["status"]    = "rejected"
    action["rejected_reason"] = reason
    action["rejected_et"]     = _et_now_str()
    _append_trade_log({
        "ts": _et_now_str(), "event": "CC_ORDER_REJECTED",
        "ticker": action["ticker"], "reason": reason,
    })
    logger.info(f"[CC] Rejected: {action['ticker']} — {reason}")
    return jsonify({"success": True, "action": action})


@app.route("/bot/pending/test/<ticker>", methods=["POST"])
def bot_pending_test(ticker):
    """Dev endpoint: manually trigger CC prep for a ticker (for testing)."""
    pos = next((p for p in _session.get("_last_positions", [])
                if p["ticker"].upper() == ticker.upper()
                and p["type"] == "STOCK"), None)
    if not pos:
        return jsonify({"error": f"{ticker} not found as STOCK in current positions"}), 404
    cost    = float(pos.get("cost", 0))
    current = float(pos.get("current", cost))
    shares  = int(pos.get("contracts", 100))
    action  = _prepare_cc_order(ticker.upper(), cost, shares, current)
    if action:
        with _pending_lock:
            _pending_actions.append(action)
        return jsonify({"success": True, "action": action})
    return jsonify({"error": "No valid CC strike found"}), 400


@app.route("/backtest/run", methods=["POST"])
def backtest_run():
    """Start a backtest in a background thread."""
    if _backtest_state["running"]:
        return jsonify({"error": "Backtest already running"}), 409

    data       = request.get_json() or {}
    capital    = float(data.get("capital", 10000))
    tickers    = data.get("tickers") or None
    start      = data.get("start")  or None
    end        = data.get("end")    or None
    save_state = bool(data.get("save_state", False))
    dte_exit   = int(data.get("dte_exit", 0))   # 0=50% only  21=dual trigger
    compare    = bool(data.get("compare", False))  # run both rules

    import threading, sys, datetime as _dt, os

    def _run():
        import sys
        orig_stdout = sys.stdout
        sys.stdout  = _LogCapture(orig_stdout)
        _backtest_state.update({"running":True,"progress":0,"log":[],"result":None,"error":None})
        try:
            sys.path.insert(0, _BASE_DIR)
            from backtest.engine import BacktestEngine

            # Primary run
            eng = BacktestEngine(
                start_date    = start,
                end_date      = end,
                capital       = capital,
                tickers       = tickers,
                max_positions = 10,
            )
            eng._dte_exit = dte_exit
            _backtest_state["log"].append(f"Starting backtest (exit rule: {'50% OR 21 DTE' if dte_exit else '50% only'})…")
            results = eng.run()
            _backtest_state["progress"] = 70

            # Optional compare run
            compare_result = None
            if compare:
                _backtest_state["log"].append("Running comparison with 50% only rule…")
                eng2 = BacktestEngine(
                    start_date    = start,
                    end_date      = end,
                    capital       = capital,
                    tickers       = tickers,
                    max_positions = 10,
                )
                eng2._dte_exit = 0   # 50% only
                results2        = eng2.run()
                compare_result  = {
                    "net_pnl":  results2.ledger.total_net_pnl(),
                    "win_rate": results2.ledger.win_rate(),
                    "cycles":   len(results2.ledger.closed_cycles()),
                }

            _backtest_state["progress"] = 85

            # Write HTML report
            rpt_dir = os.path.join(_BASE_DIR, "backtest")
            os.makedirs(rpt_dir, exist_ok=True)
            rpt_path = os.path.join(rpt_dir, "report.html")
            results.export_html(rpt_path)

            # Save wheel state if requested
            if save_state:
                from backtest.run_backtest import _save_wheel_state
                _save_wheel_state(results)

            L = results.ledger
            closed = L.closed_cycles()
            monthly = L.monthly_income()
            stats   = L.per_ticker_stats()
            curve   = L.portfolio_curve()

            # Build rich context BEFORE HTML rendering — intercepts raw CycleRecord data
            _bt_context = results.build_context(
                alt_result = compare_result,
                dte_exit   = dte_exit,
            )
            _backtest_state["_context"]     = _bt_context
            _backtest_state["_context_str"] = _bt_context.build_ollama_context()

            # Also store the thin summary dict for the UI KPI tiles (unchanged)
            _backtest_state["result"] = {
                "start":       str(results.start),
                "end":         str(results.end),
                "capital":     capital,
                "end_value":   round(capital + L.total_net_pnl(), 2),
                "net_pnl":     L.total_net_pnl(),
                "premium":     L.total_premium_collected(),
                "commissions": L.total_commissions(),
                "win_rate":    L.win_rate(),
                "assign_rate": L.assignment_rate(),
                "cycles":      len(closed),
                "monthly":     monthly,
                "per_ticker":  stats,
                "open_pos":    L.open_positions_summary(),
                "curve":       [[str(d), round(v,2)] for d,v in curve[-252:]],
                "dte_exit":    dte_exit,
                "compare":     compare_result,
                "save_state":  save_state,
            }
            _backtest_state["ran_at"] = _et_now_str()
            _session["_last_backtest"] = _backtest_state["result"]
            _backtest_state["progress"] = 100
            _backtest_state["log"].append("✓ Complete")
        except Exception as e:
            logger.error(f"Backtest error: {e}")
            _backtest_state["error"] = str(e)
            _backtest_state["log"].append(f"ERROR: {e}")
        finally:
            _backtest_state["running"] = False
            sys.stdout = orig_stdout

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "message": "Backtest started"})


@app.route("/backtest/status", methods=["GET"])
def backtest_status():
    return jsonify({
        "running":  _backtest_state["running"],
        "progress": _backtest_state["progress"],
        "log":      _backtest_state["log"][-30:],
        "result":   _backtest_state["result"],
        "ran_at":   _backtest_state["ran_at"],
        "error":    _backtest_state["error"],
    })


@app.route("/backtest/report")
def backtest_report():
    import os
    path = os.path.join(_BASE_DIR, "backtest", "report.html")
    if not os.path.exists(path):
        return "No backtest report yet.", 404
    with open(path, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/backtest/context", methods=["GET"])
def backtest_context():
    """Return the context string that would be sent to Ollama for the last backtest."""
    result = _session.get("_last_backtest") or _backtest_state.get("result")
    if not result:
        return jsonify({"context": "", "params": {}})

    bt_ctx = _backtest_state.get("_context")
    if bt_ctx is not None:
        context = _backtest_state.get("_context_str") or bt_ctx.build_ollama_context()
    else:
        r       = result
        monthly = r.get("monthly", {})
        stats   = r.get("per_ticker", {})
        compare = r.get("compare")
        mo_lines = "  ".join(f"{m}: ${v:+.0f}" for m, v in list(monthly.items())[-12:])
        top5     = list(stats.items())[:5]
        ticker_lines = "\n".join(
            f"  {t}: {s['cycles']}c  {s['net_pnl']:+.2f}  {s['win_rate']}% win  "
            f"{s['assign_rate']}% assign  {s.get('rolls',0)} rolls"
            for t, s in top5
        )
        compare_txt = ""
        if compare:
            compare_txt = (
                f"\nEXIT RULE COMPARISON:\n"
                f"  50% only:         {compare['net_pnl']:+.2f}  {compare['cycles']} cycles  {compare['win_rate']}% win\n"
                f"  50% OR 21 DTE:    {r['net_pnl']:+.2f}  {r['cycles']} cycles  {r['win_rate']}% win"
            )
        context = (
            f"BACKTEST RESULTS: {r['start']} -> {r['end']}\n"
            f"Capital: ${r['capital']:,.0f} -> ${r['end_value']:,.2f}  "
            f"({(r['end_value']-r['capital'])/r['capital']*100:.1f}%)\n"
            f"Net P&L: ${r['net_pnl']:+,.2f}  Premium: ${r['premium']:,.2f}  "
            f"Comm: ${r['commissions']:.2f}\n"
            f"Win rate: {r['win_rate']}%  Assign: {r['assign_rate']}%  "
            f"Cycles: {r['cycles']}\n{compare_txt}\n\n"
            f"PER-TICKER (top 5):\n{ticker_lines}\n\nMONTHLY:\n  {mo_lines}"
        )

    params = {}
    if result:
        params["tickers"] = result.get("tickers", [])
        params["start"]   = result.get("start", "")
        params["end"]     = result.get("end", "")
        params["capital"]  = result.get("capital", 0)
        params["cycles"]   = result.get("cycles", 0)
    return jsonify({"context": context, "params": params})



@app.route("/backtest/analyze", methods=["POST"])
def backtest_analyze():
    """Stream Ollama analysis of the last backtest result."""
    data    = request.get_json() or {}
    message = data.get("message", "").strip()
    result  = _session.get("_last_backtest") or _backtest_state.get("result")
    if not result:
        def _no_data():
            yield "data: " + _json.dumps({"error": True,
                "content": "No backtest results found. Run a backtest first."}) + "\n\n"
            yield "data: [DONE]\n\n"
        return app.response_class(_no_data(), mimetype="text/event-stream",
                                  headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    if not _ollama_reachable():
        def _no_ollama():
            yield "data: " + _json.dumps({"error": True,
                "content": "Ollama not reachable. Start with: ollama serve"}) + "\n\n"
            yield "data: [DONE]\n\n"
        return app.response_class(_no_ollama(), mimetype="text/event-stream",
                                  headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    # ── Use rich BacktestAnalysisContext if available ────────────────────
    # This contains individual CycleRecord data — not just aggregate KPIs.
    # Falls back to thin summary dict if context was not built (old runs).
    bt_ctx = _backtest_state.get("_context")
    if bt_ctx is not None:
        # Rich path: all 18 fields per trade, worst/best trades, full rolls data
        context = _backtest_state.get("_context_str") or bt_ctx.build_ollama_context()
        logger.info("backtest/analyze using rich context (%d chars, %d cycles)",
                    len(context), bt_ctx.closed_cycles)
    else:
        # Thin fallback (old backtest run, no context stored)
        r       = result
        monthly = r.get("monthly", {})
        stats   = r.get("per_ticker", {})
        compare = r.get("compare")
        mo_lines = "  ".join(f"{m}: ${v:+.0f}" for m, v in list(monthly.items())[-12:])
        top5     = list(stats.items())[:5]
        ticker_lines = "\n".join(
            f"  {t}: {s['cycles']}c  {s['net_pnl']:+.2f}  {s['win_rate']}% win  "
            f"{s['assign_rate']}% assign  {s.get('rolls',0)} rolls"
            for t, s in top5
        )
        compare_txt = ""
        if compare:
            compare_txt = (
                f"\nEXIT RULE COMPARISON:\n"
                f"  50% only:         {compare['net_pnl']:+.2f}  {compare['cycles']} cycles  {compare['win_rate']}% win\n"
                f"  50% OR 21 DTE:    {r['net_pnl']:+.2f}  {r['cycles']} cycles  {r['win_rate']}% win"
            )
        context = (
            f"BACKTEST RESULTS: {r['start']} → {r['end']}\n"
            f"Capital: ${r['capital']:,.0f} → ${r['end_value']:,.2f}  "
            f"({(r['end_value']-r['capital'])/r['capital']*100:.1f}%)\n"
            f"Net P&L: ${r['net_pnl']:+,.2f}  Premium: ${r['premium']:,.2f}  "
            f"Comm: ${r['commissions']:.2f}\n"
            f"Win rate: {r['win_rate']}%  Assign: {r['assign_rate']}%  "
            f"Cycles: {r['cycles']}\n{compare_txt}\n\n"
            f"PER-TICKER (top 5):\n{ticker_lines}\n\nMONTHLY:\n  {mo_lines}"
        )
        logger.info("backtest/analyze using thin fallback context")

    system = """You are a wheel strategy performance analyst reviewing historical backtest results.
You have access to individual trade records — reference specific trades, dates, and amounts.
Do not give generic options education. The user is an experienced wheel trader.
When asked what failed, cite the specific worst trades by ticker, date, and dollar amount.
When asked what worked, cite the specific best trades.
Keep responses concise — 3-5 sentences per section, bullet points where helpful."""

    user_msg = message if message else (
        "Analyse these backtest results. Cover: overall performance vs expectations, "
        "best and worst tickers with reasons, impact of the exit rule if compared, "
        "and 2-3 specific recommendations to improve results going forward."
    )

    messages = [
        {"role": "system",    "content": system},
        {"role": "user",      "content": context + "\n\n" + user_msg},
    ]
    return app.response_class(
        _stream_ollama(messages),
        mimetype="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"}
    )
