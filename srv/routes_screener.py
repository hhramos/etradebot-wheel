"""/screener*, /run — extracted verbatim from server.py."""
from srv.core import (
    _BASE_DIR,
    _annotate_candidates, _session, app, jsonify, logger, os,
    request,
)


@app.route("/screener", methods=["GET"])
def screener():
    try:
        import sys
        sys.path.insert(0, _BASE_DIR)
        from wheel_screener import WheelScreener
        nav = _session.get("_net_value", 0) or 0
        screener_obj = WheelScreener(
            account_nav         = nav,
            min_fisher          = 6,
            min_wheel_grade     = "C",
        )
        result = screener_obj.run()
        _annotate_candidates(result)
        _session["_screener_cache"] = {c["ticker"]: c for c in result}
        # VIX regime indicator (best-effort — never blocks screener)
        vix_level = None
        try:
            import yfinance as _yf
            _vi = _yf.Ticker("^VIX")
            vix_level = round(float(
                _vi.fast_info.get("last_price") or
                _vi.info.get("regularMarketPrice") or 0), 2) or None
        except Exception:
            vix_level = None
        return jsonify({"success": True, "candidates": result,
                        "vix": vix_level, "account_net_value": nav})
    except Exception as e:
        logger.warning(f"Live screener failed ({e}), using demo data")
        candidates = [
            {"ticker":"KO",  "price":46.80,"fisher_score":11,"wheel_grade":"A","wheel_score":91,"iv_pct":16,"annual_div_pct":3.2,"pctOfPortfolio":0.145,"csp_strike":45.0,"csp_expiry":"2026-06-19"},
            {"ticker":"USB", "price":37.50,"fisher_score":10,"wheel_grade":"A","wheel_score":89,"iv_pct":25,"annual_div_pct":4.6,"pctOfPortfolio":0.140,"csp_strike":36.0,"csp_expiry":"2026-06-19"},
            {"ticker":"CSCO","price":48.90,"fisher_score":10,"wheel_grade":"A","wheel_score":87,"iv_pct":21,"annual_div_pct":3.3,"pctOfPortfolio":0.138,"csp_strike":47.0,"csp_expiry":"2026-06-19"},
            {"ticker":"BAC", "price":38.20,"fisher_score": 9,"wheel_grade":"A","wheel_score":88,"iv_pct":24,"annual_div_pct":2.6,"pctOfPortfolio":0.136,"csp_strike":37.0,"csp_expiry":"2026-06-19"},
            {"ticker":"VZ",  "price":40.60,"fisher_score": 8,"wheel_grade":"A","wheel_score":86,"iv_pct":18,"annual_div_pct":6.5,"pctOfPortfolio":0.132,"csp_strike":39.0,"csp_expiry":"2026-06-19"},
            {"ticker":"PFE", "price":27.40,"fisher_score": 9,"wheel_grade":"A","wheel_score":84,"iv_pct":28,"annual_div_pct":5.9,"pctOfPortfolio":0.128,"csp_strike":26.0,"csp_expiry":"2026-06-19"},
            {"ticker":"WFC", "price":47.60,"fisher_score": 8,"wheel_grade":"B","wheel_score":79,"iv_pct":26,"annual_div_pct":2.9,"pctOfPortfolio":0.120,"csp_strike":46.0,"csp_expiry":"2026-06-19"},
            {"ticker":"MO",  "price":44.10,"fisher_score": 7,"wheel_grade":"B","wheel_score":75,"iv_pct":20,"annual_div_pct":8.4,"pctOfPortfolio":0.112,"csp_strike":43.0,"csp_expiry":"2026-06-19"},
        ]
        _annotate_candidates(candidates)
        _session["_screener_cache"] = {c["ticker"]: c for c in candidates}
        # VIX regime indicator (best-effort — never blocks screener)
        vix_level = None
        try:
            import yfinance as _yf
            _vi = _yf.Ticker("^VIX")
            vix_level = round(float(
                _vi.fast_info.get("last_price") or
                _vi.info.get("regularMarketPrice") or 0), 2) or None
        except Exception:
            vix_level = None
        return jsonify({"success": True, "demo": True, "candidates": candidates,
                        "vix": vix_level, "account_net_value": _session.get("_net_value", 0)})


@app.route("/run", methods=["POST"])
def run_bot():
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401
    data    = request.get_json()
    preview = data.get("preview", True)
    logger.info(f"Bot run requested — preview={preview}")
    try:
        import sys
        sys.path.insert(0, ".")
        from bot import Bot
        bot    = Bot(preview=preview)
        result = bot.run()
        return jsonify({"success": True, "preview": preview, "result": str(result)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "demo": True,
                        "message": "Demo mode — bot.run() requires full etradebot install"})


@app.route("/screener/signals/<ticker>", methods=["GET"])
def screener_signals(ticker):
    """Fetch insider, political, and social signals for a single ticker."""
    try:
        from wheel_screener import fetch_signals
        signals = fetch_signals(ticker.upper(), force=request.args.get("force")=="1")
        return jsonify({"ticker": ticker.upper(), "signals": signals})
    except Exception as e:
        return jsonify({"ticker": ticker.upper(), "signals": {
            "insider":   {"signal": "none", "label": "—"},
            "political": {"signal": "none", "label": "—"},
            "social":    {"signal": "none", "label": "—"},
            "error":     str(e),
        }})


@app.route("/screener/signals/batch", methods=["POST"])
def screener_signals_batch():
    """Fetch signals for multiple tickers at once."""
    data    = request.get_json()
    tickers = [t.upper() for t in (data.get("tickers") or [])][:20]
    if not tickers:
        return jsonify({"error": "tickers list required"}), 400
    try:
        from wheel_screener import fetch_signals_batch
        results = fetch_signals_batch(tickers)
        return jsonify({"signals": results})
    except Exception as e:
        return jsonify({"error": str(e), "signals": {}})


@app.route("/screener/quote/<ticker>", methods=["GET"])
def screener_quote(ticker):
    """
    Return screener data for a single ticker.
    Checks session cache first; fetches live via WheelScreener if missing.
    Used by the custom CSP entry card.
    """
    ticker = ticker.upper().strip()
    # Cache hit
    cached = _session.get("_screener_cache", {}).get(ticker)
    if cached and cached.get("price"):
        return jsonify({"cached": True, **cached})
    # Live fetch
    try:
        from wheel_screener import WheelScreener
        nav = _session.get("_net_value", 0) or 0
        result = WheelScreener(
            universe=[ticker], account_nav=nav,
            min_fisher=0, min_wheel_grade="D"
        ).run()
        if result:
            c = result[0]
            _session.setdefault("_screener_cache", {})[ticker] = c
            return jsonify({"cached": False, **c})
        return jsonify({"error": f"No options data found for {ticker}",
                        "ticker": ticker}), 404
    except Exception as e:
        logger.warning(f"screener_quote {ticker}: {e}")
        return jsonify({"error": str(e), "ticker": ticker}), 500
