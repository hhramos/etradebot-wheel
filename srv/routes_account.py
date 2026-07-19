"""Read endpoints: /account, /positions, /orders, /market/quote — extracted verbatim from server.py."""
from srv.core import (
    _classify_position, _exit_reason, _session, app, jsonify, logger,
)


@app.route("/account", methods=["GET"])
def account():
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401
    try:
        import pyetrade
        api = pyetrade.ETradeAccounts(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        raw = api.get_account_balance(_session["account_id"], resp_format="json")
        logger.info(f"Account balance raw keys: {list(raw.keys())}")

        # Parse E-Trade BalanceResponse JSON structure
        br = raw.get("BalanceResponse", raw)
        computed = br.get("Computed", {})
        rtv = computed.get("RealTimeValues", {})

        buying_power  = (computed.get("marginBuyingPower")
                      or computed.get("cashBuyingPower")
                      or computed.get("cashAvailableForInvestment") or 0)
        net_value     = (rtv.get("totalAccountValue")
                      or computed.get("accountBalance")
                      or computed.get("regtEquity") or 0)
        cash_balance  = (computed.get("cashBalance")
                      or computed.get("netCash") or 0)
        margin_bal    = computed.get("marginBalance") or 0

        _session["_net_value"] = float(net_value)
        _session["_last_account"] = {          # used by advisor prompt
            "net_value":    float(net_value),
            "buying_power": float(buying_power),
            "cash_balance": float(cash_balance),
        }
        return jsonify({
            "buying_power":   float(buying_power),
            "net_value":      float(net_value),
            "cash_balance":   float(cash_balance),
            "margin_balance": float(margin_bal),
            "_raw":           raw,
        })
    except Exception as e:
        logger.error(f"Account balance error: {e}")
        if "401" in str(e):
            _session["_token_expired"] = True
            logger.warning("[AUTH] 401 on account — token expired, banner triggered")
        return jsonify({"error": str(e), "buying_power": 0, "net_value": 0,
                        "cash_balance": 0, "margin_balance": 0})


@app.route("/positions/raw", methods=["GET"])
def positions_raw():
    """Debug endpoint — returns the raw E-Trade portfolio response."""
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401
    try:
        import pyetrade
        api = pyetrade.ETradeAccounts(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        raw = api.get_account_portfolio(_session["account_id"], resp_format="json")
        return jsonify(raw)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/positions", methods=["GET"])
def positions():
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401
    try:
        import pyetrade
        api = pyetrade.ETradeAccounts(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        raw = api.get_account_portfolio(
            _session["account_id"],
            resp_format="json",
            totals_required=True,
        )
        logger.info(f"Portfolio raw keys: {list(raw.keys())}")

        # Parse E-Trade PortfolioResponse structure
        pr        = raw.get("PortfolioResponse", raw)
        acct_port = pr.get("AccountPortfolio", [])
        if isinstance(acct_port, dict):
            acct_port = [acct_port]

        positions_out = []
        for acct in acct_port:
            raw_positions = acct.get("Position", [])
            if isinstance(raw_positions, dict):
                raw_positions = [raw_positions]
            for p in raw_positions:
                product  = p.get("Product", p.get("product", {}))
                quick    = p.get("Quick",   p.get("quick",   {})) or {}

                # E-Trade uses securityType on the Product for options ("OPTN")
                # but typeCode can be "EQUITY" even for option rows — use securityType
                sec_type = product.get("securityType", "EQ")
                ticker   = product.get("symbol", "???")

                # quantity is negative for short positions (sold options)
                qty_raw  = float(p.get("quantity", 0) or 0)
                qty      = qty_raw  # preserve sign for cost basis calc

                cost     = float(p.get("costPerShare", p.get("pricePaid", 0)) or 0)
                current  = float(quick.get("lastTrade", cost) or cost)
                pnl      = float(p.get("totalGain",  p.get("daysGain", 0)) or 0)
                pnl_pct  = float(p.get("totalGainPct", 0) or 0)

                if sec_type == "OPTN":
                    call_put = product.get("callPut", "")
                    pos_type = "CSP" if call_put == "PUT" else "CC"
                    strike   = float(product.get("strikePrice", 0) or 0)

                    # expiryYear/Month/Day come from symbolDescription or osiKey
                    # Fall back to parsing symbolDescription: e.g. "CCL Aug 21 '26 $30 Call"
                    ey = int(product.get("expiryYear",  0) or 0)
                    em = int(product.get("expiryMonth", 0) or 0)
                    ed = int(product.get("expiryDay",   0) or 0)

                    if ey and em and ed:
                        expiry = f"{ey}-{str(em).zfill(2)}-{str(ed).zfill(2)}"
                    else:
                        # Try to parse from symbolDescription
                        import re
                        sym_desc = p.get("symbolDescription", "")
                        m = re.search(r"([A-Za-z]+)\s+(\d+)\s+'(\d+)", sym_desc)
                        if m:
                            months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                                      "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
                            mo = months.get(m.group(1), 0)
                            expiry = f"20{m.group(3)}-{str(mo).zfill(2)}-{m.group(2).zfill(2)}"
                        else:
                            expiry = ""

                    contracts = int(abs(qty_raw)) or 1
                else:
                    pos_type  = "STOCK"
                    strike    = 0
                    expiry    = ""
                    contracts = int(abs(qty_raw))

                sc = _session.get("_screener_cache", {}).get(ticker, {})
                positions_out.append({
                    "ticker":       ticker,
                    "type":         pos_type,
                    "strike":       strike,
                    "expiry":       expiry,
                    "contracts":    contracts,
                    "cost":         round(cost, 2),
                    "current":      round(current, 2),
                    "iv":           round(float(sc.get("iv_pct", 0) or 0), 1),
                    "pnl":          round(pnl, 2),
                    "pnl_pct":      round(pnl_pct, 1),
                    "action":           _classify_position(
                                            p, qty_raw,
                                            cost    = cost,
                                            current = current,
                                            expiry  = expiry,
                                        ),
                    "exit_reason":      _exit_reason(cost, current, expiry),
                    "full_contracts":    contracts // 100 if pos_type == "STOCK" else contracts,
                    "leftover_shares":  contracts % 100  if pos_type == "STOCK" else 0,
                    "fisher_score": sc.get("fisher_score"),
                    "wheel_score":  sc.get("wheel_score"),
                    "wheel_grade":  sc.get("wheel_grade"),
                    "sector":       _session.get("_sector_map",{}).get(ticker,""),
                })

        # ── Post-process: if a STOCK has an open CC for same ticker → HOLD_CC
        open_cc_tickers = {
            p["ticker"] for p in positions_out
            if p.get("type") == "CC"
        }
        for p in positions_out:
            if (p.get("type") == "STOCK"
                    and p.get("action") in ("SELL_CC", "SELL_CC_PARTIAL")
                    and p["ticker"] in open_cc_tickers):
                p["action"]     = "HOLD_CC"
                p["cc_exists"]  = True

        # ── Sort: CC before STOCK for same ticker so "manage CC row above" is correct
        def _sort_key(p):
            t = p.get("ticker", "")
            if p.get("type") == "CC":    return (t, 0)   # CC first
            if p.get("type") == "STOCK": return (t, 2)   # STOCK after CC
            return (t, 1)                                  # CSP between
        positions_out.sort(key=_sort_key)

        # Supplemental scoring: fetch fisher/wheel for any tickers not in screener cache
        missing = [p["ticker"] for p in positions_out
                   if p["fisher_score"] is None]
        if missing:
            try:
                from wheel_screener import WheelScreener, fisher_score as fs_fn,                     wheel_score_and_grade, estimate_iv_from_hist_vol
                import yfinance as yf
                for sym in missing:
                    try:
                        t    = yf.Ticker(sym)
                        info = t.info
                        iv   = estimate_iv_from_hist_vol(t)
                        f    = fs_fn(info)
                        w, g = wheel_score_and_grade(info, iv)
                        # Update the matching position dict
                        for pos in positions_out:
                            if pos["ticker"] == sym:
                                pos["fisher_score"] = f
                                pos["wheel_score"]  = w
                                pos["wheel_grade"]  = g
                        # Also cache it for future use
                        _session.setdefault("_screener_cache", {})[sym] = {
                            "fisher_score": f, "wheel_score": w, "wheel_grade": g
                        }
                    except Exception as e2:
                        logger.warning(f"Supplemental score failed for {sym}: {e2}")
            except ImportError:
                pass   # yfinance not installed, leave as None

        _session["_last_positions"] = positions_out   # used by advisor prompt
        return jsonify({"positions": positions_out})

    except Exception as e:
        logger.error(f"Positions error: {e}")
        if "401" in str(e):
            _session["_token_expired"] = True
            logger.warning("[AUTH] 401 on positions — token expired, banner triggered")
        return jsonify({"error": str(e), "positions": []})


@app.route("/orders", methods=["GET"])
def orders():
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401
    try:
        import pyetrade, re as _re
        api = pyetrade.ETradeOrder(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        raw = api.list_orders(_session["account_id"], resp_format="json")
        order_list = (raw.get("OrdersResponse", {}).get("Order", [])
                      if isinstance(raw, dict) else [])
        if isinstance(order_list, dict):
            order_list = [order_list]
        normalized = []
        for o in order_list:
            detail = o.get("OrderDetail", [{}])
            if isinstance(detail, list): detail = detail[0] if detail else {}
            instr  = detail.get("Instrument", [{}])
            if isinstance(instr,  list): instr  = instr[0]  if instr  else {}
            product = instr.get("Product", {})
            status  = o.get("status", detail.get("status", ""))
            # Parse expiry
            ey = int(product.get("expiryYear",  0) or 0)
            em = int(product.get("expiryMonth", 0) or 0)
            ed = int(product.get("expiryDay",   0) or 0)
            expiry = (f"{ey}-{str(em).zfill(2)}-{str(ed).zfill(2)}"
                      if ey and em and ed else "")
            # Placed timestamp
            placed_ts = o.get("placedTime", "")
            placed_at = placed_ts[:10] if placed_ts else ""
            normalized.append({
                "order_id":    str(o.get("orderId", "")),
                "ticker":      product.get("symbol", ""),
                "action":      instr.get("orderAction", ""),
                "option_type": product.get("callPut", ""),
                "strike":      float(product.get("strikePrice", 0) or 0),
                "expiry":      expiry,
                "contracts":   int(instr.get("orderedQuantity",
                                             instr.get("filledQuantity", 0)) or 0),
                "limit_price": float(detail.get("limitPrice", 0) or 0),
                "tif":         detail.get("orderTerm", "DAY"),
                "status":      status.upper() if status else "",
                "placed_at":   placed_at,
            })
        return jsonify({"orders": normalized})
    except Exception as e:
        logger.error(f"Orders fetch failed: {e}")
        if "401" in str(e):
            _session["_token_expired"] = True
        # Return empty + error — a fabricated demo order would render as a
        # phantom GTC row under real positions in the UI.
        return jsonify({"orders": [], "error": str(e)})


@app.route("/market/quote/<ticker>", methods=["GET"])
def market_quote(ticker):
    """
    Current underlying stock price for the positions table price column.
    Source 1: screener cache (zero extra API calls, up to 30 min stale)
    Source 2: E*Trade live quote (fresh, requires connection)
    """
    ticker = ticker.upper().strip()

    # Source 1 — screener cache (free, no API call)
    cache      = _session.get("_screener_cache", {})
    candidates = cache.get("candidates", [])
    for c in candidates:
        if str(c.get("ticker","")).upper() == ticker:
            return jsonify({
                "ticker":     ticker,
                "price":      c.get("price"),
                "change_pct": None,   # screener cache has no intraday change
                "source":     "screener_cache",
            })

    # Source 2 — E*Trade live quote
    if not _session["connected"]:
        return jsonify({"ticker": ticker, "price": None,
                        "error": "not connected"}), 200   # 200 so UI handles gracefully

    try:
        from requests_oauthlib import OAuth1Session
        sess = OAuth1Session(
            _session["consumer_key"],
            client_secret         = _session["consumer_secret"],
            resource_owner_key    = _session["access_token"],
            resource_owner_secret = _session["access_token_secret"],
        )
        url  = f"https://api.etrade.com/v1/market/quote/{ticker}"
        resp = sess.get(url,
                        headers={"Accept": "application/json"},
                        params={"detailFlag": "ALL"},   # ALL returns price, prev close, bid/ask
                        timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            qd   = (data.get("QuoteResponse", {})
                        .get("QuoteData", [{}])[0]
                        .get("All", {}))
            logger.debug(f"Quote {ticker} keys: {list(qd.keys())[:10]} "
                         f"lastTrade={qd.get('lastTrade')} prev={qd.get('previousClose')}")
            last_trade = qd.get("lastTrade")
            prev_cls   = qd.get("previousClose")
            bid        = qd.get("bid")
            ask        = qd.get("ask")
            # After market close lastTrade is None — fall back to prev close
            if last_trade and float(last_trade or 0) > 0:
                price = float(last_trade)
            elif prev_cls and float(prev_cls or 0) > 0:
                price = float(prev_cls)   # closing price for after-hours display
            elif bid and ask:
                price = round((float(bid) + float(ask)) / 2, 2)
            else:
                price = None
            chg_pct = qd.get("changeClose")
            if price and prev_cls and float(prev_cls or 0) > 0 and not chg_pct:
                try:
                    chg_pct = round((price - float(prev_cls)) / float(prev_cls) * 100, 2)
                except Exception:
                    chg_pct = None
            logger.warning(f"market/quote/{ticker} HTTP {resp.status_code}")
            # Fall through to yfinance fallback below
        else:
            # E*Trade returned 200 but price still None — fall through to yfinance
            if price is not None:
                return jsonify({
                    "ticker":     ticker,
                    "price":      price,
                    "change_pct": chg_pct,
                    "prev_close": float(prev_cls) if prev_cls else None,
                    "source":     "etrade_live",
                    "after_hours": not bool(last_trade and float(last_trade or 0) > 0),
                })
            logger.warning(f"market/quote/{ticker}: E*Trade returned no price — trying yfinance")
    except Exception as e:
        logger.warning(f"market/quote/{ticker} E*Trade error: {e} — trying yfinance")

    # ── Fallback: yfinance (works after hours, uses last close) ──────────────
    try:
        import yfinance as _yf
        _t = _yf.Ticker(ticker)
        _info = _t.fast_info   # lightweight, no full info fetch
        _price = getattr(_info, "last_price", None) or getattr(_info, "previous_close", None)
        _prev  = getattr(_info, "previous_close", None)
        _chg   = None
        if _price and _prev and _prev > 0:
            _chg = round((_price - _prev) / _prev * 100, 2)
        if _price:
            logger.info(f"market/quote/{ticker}: yfinance fallback price={_price}")
            return jsonify({
                "ticker":     ticker,
                "price":      round(float(_price), 2),
                "change_pct": _chg,
                "prev_close": round(float(_prev), 2) if _prev else None,
                "source":     "yfinance",
                "after_hours": True,
            })
    except Exception as yfe:
        logger.warning(f"market/quote/{ticker} yfinance fallback error: {yfe}")

    return jsonify({"ticker": ticker, "price": None, "error": "no price source available"}), 200


@app.route("/orders/<order_id>", methods=["DELETE"])
def cancel_order(order_id):
    """Cancel an open order by ID."""
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401
    try:
        import pyetrade
        api = pyetrade.ETradeOrder(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        result = api.cancel_order(_session["account_id"], order_id)
        logger.info(f"Cancelled order {order_id}")
        return jsonify({"success": True, "cancelled_id": order_id, "result": result})
    except Exception as e:
        logger.error(f"Cancel order {order_id} failed: {e}")
        return jsonify({"success": False, "error": str(e)})
