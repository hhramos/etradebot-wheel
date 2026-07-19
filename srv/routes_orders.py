"""Order placement: /order/preview, /order/submit, /order/chain, /order/roll — extracted verbatim from server.py."""
from srv.core import (
    _append_trade_log, _et_now_str, _session, app, datetime, jsonify,
    logger, request,
)


@app.route("/order/preview", methods=["POST"])
def order_preview():
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401
    order = request.get_json()
    # Mirror back with a simulated preview ID
    order["preview_id"] = "PRV" + str(abs(hash(str(order))))[:6]
    order["estimated_commission"] = 0.65
    return jsonify({"success": True, "preview": order})


@app.route("/order/submit", methods=["POST"])
def order_submit():
    """
    Place an option order via E*Trade XML API using the required two-step flow:
      Step 1: POST /orders/preview  → get previewId (prevents error 101)
      Step 2: POST /orders/place    → submit with previewId

    Fix A: preview step eliminates error 101 "timed out your original order request"
    Fix B: UUID-based clientOrderId prevents collision on retry within same second

    Uses raw XML + requests-oauthlib — bypasses pyetrade's parameter validation.
    """
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401
    order = request.get_json()
    logger.info(f"ORDER SUBMIT: {order}")

    ticker      = order.get("ticker","").upper()
    action      = order.get("action","")
    option_type = order.get("option_type","PUT").upper()
    strike      = float(order.get("strike", 0))
    expiry      = order.get("expiry","")
    contracts   = int(order.get("contracts", 1))
    limit_price = float(order.get("limit_price", 0))
    _TIF_MAP    = {"GTC": "GOOD_UNTIL_CANCEL", "DAY": "GOOD_FOR_DAY",
                   "IOC": "IMMEDIATE_OR_CANCEL", "FOK": "FILL_OR_KILL"}
    tif_raw  = order.get("tif", "DAY").upper()
    tif      = _TIF_MAP.get(tif_raw, tif_raw)
    account_id  = _session.get("account_id","")

    exp_parts = expiry.split("-") if expiry else []
    if len(exp_parts) == 3:
        exp_year, exp_month, exp_day = exp_parts
    else:
        exp_year = exp_month = exp_day = ""

    # Fix B: UUID ensures unique clientOrderId — no collision on retry
    import uuid as _uuid
    client_id = str(_uuid.uuid4()).replace("-","")[:20]

    def _build_xml(order_term_override=None, root_tag="PlaceOrderRequest"):
        """Build the XML order body. root_tag should be PreviewOrderRequest for
        the preview step and PlaceOrderRequest for the place step."""
        term = order_term_override or tif
        return f"""<?xml version="1.0" encoding="utf-8"?>
<{root_tag}>
  <orderType>OPTN</orderType>
  <clientOrderId>{client_id}</clientOrderId>
  <Order>
    <allOrNone>false</allOrNone>
    <priceType>LIMIT</priceType>
    <orderTerm>{term}</orderTerm>
    <marketSession>REGULAR</marketSession>
    <stopPrice/>
    <limitPrice>{limit_price:.2f}</limitPrice>
    <Instrument>
      <Product>
        <securityType>OPTN</securityType>
        <symbol>{ticker}</symbol>
        <callPut>{option_type}</callPut>
        <expiryYear>{exp_year}</expiryYear>
        <expiryMonth>{exp_month}</expiryMonth>
        <expiryDay>{exp_day}</expiryDay>
        <strikePrice>{strike:.1f}</strikePrice>
      </Product>
      <orderAction>{action}</orderAction>
      <quantityType>QUANTITY</quantityType>
      <quantity>{contracts}</quantity>
    </Instrument>
  </Order>
</{root_tag}>"""

    try:
        from requests_oauthlib import OAuth1Session
        import xml.etree.ElementTree as _ET

        sess = OAuth1Session(
            _session["consumer_key"],
            client_secret         = _session["consumer_secret"],
            resource_owner_key    = _session["access_token"],
            resource_owner_secret = _session["access_token_secret"],
        )
        headers = {"Content-Type": "application/xml", "Accept": "application/json"}
        base    = f"https://api.etrade.com/v1/accounts/{account_id}/orders"

        # ── Step 1: Preview (Fix A — eliminates error 101) ──────────────────
        preview_xml = _build_xml(root_tag="PreviewOrderRequest")
        logger.info(f"Preview XML:\n{preview_xml}")
        preview_resp = sess.post(
            f"{base}/preview",
            data    = preview_xml,
            headers = headers,
            timeout = 30,
        )
        if preview_resp.status_code == 401:
            _session["_token_expired"] = True
            return jsonify({"success": False, "error": "Token expired — re-authenticate"}), 401

        if preview_resp.status_code not in (200, 201):
            raw = preview_resp.text[:500]
            logger.error(f"Order preview HTTP {preview_resp.status_code}: {raw}")
            # Try to parse E*Trade XML error for a human-readable message
            err_msg = f"E*Trade preview returned {preview_resp.status_code}"
            try:
                root = _ET.fromstring(preview_resp.text)
                code = root.findtext("code") or root.findtext(".//code") or "?"
                msg  = root.findtext("message") or root.findtext(".//message") or ""
                err_msg = f"E*Trade error {code}" + (f": {msg}" if msg else "")
            except Exception:
                pass
            return jsonify({"success": False,
                            "error": err_msg,
                            "detail": raw}), 400

        # Extract previewId from preview response
        preview_id = None
        try:
            pr = preview_resp.json()
            logger.info(f"Order preview response: {pr}")
            pids = pr.get("PreviewOrderResponse", {}).get("PreviewIds", [])
            if isinstance(pids, list) and pids:
                preview_id = pids[0].get("previewId")
            elif isinstance(pids, dict):
                preview_id = pids.get("previewId")
        except Exception as pe:
            logger.warning(f"Order preview parse error: {pe}")

        logger.info(f"Order preview OK — previewId={preview_id}")

        if not preview_id:
            return jsonify({"success": False,
                            "error": "Preview did not return a previewId — order not placed",
                            "detail": "Check server log for the full preview response"}), 400

        # ── Step 2: Place with previewId ─────────────────────────────────────
        # Inject previewId into the XML for the place call
        place_xml = _build_xml(root_tag="PlaceOrderRequest")
        if preview_id:
            place_xml = place_xml.replace(
                "</PlaceOrderRequest>",
                f"  <PreviewIds><previewId>{preview_id}</previewId>"
                f"<cashMargin>CASH</cashMargin></PreviewIds>\n</PlaceOrderRequest>"
            )
        logger.info(f"Place XML:\n{place_xml}")

        place_resp = sess.post(
            f"{base}/place",
            data    = place_xml,
            headers = headers,
            timeout = 30,
        )
        if place_resp.status_code == 401:
            _session["_token_expired"] = True
            return jsonify({"success": False, "error": "Token expired — re-authenticate"}), 401

        if place_resp.status_code not in (200, 201):
            raw = place_resp.text[:500]
            logger.error(f"Order place HTTP {place_resp.status_code}: {raw}")
            err_msg = f"E*Trade place returned {place_resp.status_code}"
            try:
                root = _ET.fromstring(place_resp.text)
                code = root.findtext("code") or root.findtext(".//code") or "?"
                msg  = root.findtext("message") or root.findtext(".//message") or ""
                err_msg = f"E*Trade error {code}" + (f": {msg}" if msg else "")
            except Exception:
                pass
            return jsonify({"success": False,
                            "error": err_msg,
                            "detail": raw}), 400

        result   = place_resp.json()
        oids = result.get("PlaceOrderResponse", {}).get("OrderIds", {})
        if isinstance(oids, list) and oids:
            ids = oids[0]
        elif isinstance(oids, dict):
            ids = oids
        else:
            ids = {}
        order_id = ids.get("orderId") or ids.get("OrderId") or "?"
        logger.info(f"Order placed: {action} {ticker} {option_type} "
                    f"${strike} {expiry} ×{contracts}c @ ${limit_price} {tif} → #{order_id}")
        return jsonify({"success": True, "order_id": str(order_id), "result": result})

    except Exception as e:
        logger.error(f"Order submit error: {e}")
        return jsonify({"success": False,
                        "error": str(e),
                        "detail": "Order was NOT placed. Check server log for details."}), 500


@app.route("/order/chain/<ticker>", methods=["GET"])
def option_chain(ticker):
    """
    Fetch live option chain for a ticker.
    Returns puts or calls for the next two monthly expiries with greeks.
    Used by the roll panel to let user pick new strike/expiry.
    """
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401

    option_type = request.args.get("type", "PUT").upper()    # PUT or CALL
    ticker      = ticker.upper()

    try:
        from bot.time_utils import next_monthly_expiry, days_to_expiry
        # Build list of next 3 monthly expiries to offer
        expiries = []
        for offset in range(0, 4):
            exp = next_monthly_expiry(offset)
            dte = days_to_expiry(exp)
            if dte >= 7:   # skip expiries too close
                expiries.append(exp.isoformat())
            if len(expiries) == 3:
                break
    except Exception:
        import datetime
        today = datetime.date.today()
        expiries = [
            (today.replace(day=1) + datetime.timedelta(days=32 * i)).replace(day=19).isoformat()
            for i in range(1, 4)
        ]

    try:
        import pyetrade
        api = pyetrade.ETradeMarket(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        all_strikes = []
        for exp in expiries[:2]:   # fetch 2 expiries to keep response fast
            parts = exp.split("-")
            if len(parts) != 3:
                continue
            ey, em, ed = parts
            try:
                import datetime as _dt_chain
                raw = api.get_option_chains(
                    underlier          = ticker,
                    expiry_date        = _dt_chain.date.fromisoformat(exp),  # must be date object not string
                    option_category    = "STANDARD",
                    chain_type         = option_type,
                    skip_adjusted      = True,
                    no_of_strikes      = 20,
                    resp_format        = "json",
                )
                resp  = raw.get("OptionChainResponse", {})
                pairs = resp.get("OptionPair", [])
                if isinstance(pairs, dict):
                    pairs = [pairs]
                key = "Put" if option_type == "PUT" else "Call"
                for pair in pairs:
                    opt = pair.get(key, {})
                    if not opt:
                        continue
                    bid = float(opt.get("bid", 0) or 0)
                    ask = float(opt.get("ask", 0) or 0)
                    mid = round((bid + ask) / 2, 2) if bid and ask else 0
                    oi  = int(opt.get("openInterest", 0) or 0)
                    if mid < 0.05 or oi < 10:
                        continue
                    all_strikes.append({
                        "expiry":         exp,
                        "strike":         float(opt.get("strikePrice", 0) or 0),
                        "bid":            bid,
                        "ask":            ask,
                        "mid":            mid,
                        "delta":          float(opt.get("delta", 0) or 0),
                        "iv":             float(opt.get("impliedVolatility", 0) or 0),
                        "open_interest":  oi,
                        "volume":         int(opt.get("volume", 0) or 0),
                    })
            except Exception as e:
                logger.warning(f"Chain fetch {ticker} {exp}: {e}")

        if all_strikes:
            all_strikes.sort(key=lambda x: (x["expiry"], x["strike"]))
            return jsonify({"ticker": ticker, "type": option_type,
                            "strikes": all_strikes, "expiries": expiries[:2]})

        # Fallback: estimate from IV in screener cache
        raise ValueError("No live chain data")

    except Exception as e:
        logger.warning(f"option_chain live fallback ({e}) — estimating from IV")
        # Deterministic fallback from screener cache
        sc      = _session.get("_screener_cache", {}).get(ticker, {})
        iv_pct  = float(sc.get("iv_pct", 25) or 25)
        price   = float(sc.get("price", 20) or 20)
        from bot.time_utils import next_monthly_expiry, days_to_expiry
        strikes_out = []
        for offset in range(1, 3):
            exp = next_monthly_expiry(offset)
            exp_str = exp.isoformat()
            dte = days_to_expiry(exp)
            base = round(price / 0.5) * 0.5
            for delta_s in range(-5, 6):
                s     = round(base + delta_s * 0.5, 2)
                if s <= 0:
                    continue
                prem  = round(iv_pct / 500 * s * (dte / 30) ** 0.5, 2)
                if prem < 0.05:
                    continue
                delta = round(max(0.05, min(0.50, 0.30 - delta_s * 0.05)), 2)
                strikes_out.append({
                    "expiry": exp_str, "strike": s,
                    "bid": round(prem * 0.9, 2), "ask": round(prem * 1.1, 2),
                    "mid": prem, "delta": delta, "iv": iv_pct / 100,
                    "open_interest": 200, "volume": 50,
                })
        return jsonify({"ticker": ticker, "type": option_type,
                        "strikes": strikes_out, "expiries": expiries[:2],
                        "estimated": True})


@app.route("/order/roll", methods=["POST"])
def order_roll():
    """
    Execute a roll: Buy-to-close the current position AND Sell-to-open
    a new position at a different strike/expiry.
    Submits both legs as sequential orders.
    Returns {success, btc_order_id, sto_order_id, net_credit, detail}
    """
    if not _session["connected"]:
        return jsonify({"error": "Not connected"}), 401

    data = request.get_json()
    logger.info(f"ROLL REQUEST: {data}")

    # Current position (leg to close)
    ticker        = data.get("ticker","").upper()
    option_type   = data.get("option_type","PUT").upper()
    cur_strike    = float(data.get("cur_strike", 0))
    cur_expiry    = data.get("cur_expiry","")
    contracts     = int(data.get("contracts", 1))
    btc_limit     = float(data.get("btc_limit", 0))   # debit to pay

    # New position (leg to open)
    new_strike    = float(data.get("new_strike", 0))
    new_expiry    = data.get("new_expiry","")
    sto_limit     = float(data.get("sto_limit", 0))    # credit to receive

    account_id    = _session.get("account_id","")
    import time as _t

    def _parse_expiry(exp):
        parts = exp.split("-") if exp else []
        if len(parts) == 3:
            return parts
        return ["","",""]

    def _place_leg(action, strike, expiry, limit, leg_label):
        ey, em, ed = _parse_expiry(expiry)
        # Fix B: UUID clientOrderId — no collision on retry
        import uuid as _uuid
        client_id = str(_uuid.uuid4()).replace("-","")[:20]

        def _xml(preview_id=None, root_tag="PlaceOrderRequest"):
            base = f"""<?xml version="1.0" encoding="utf-8"?>
<{root_tag}>
  <orderType>OPTN</orderType>
  <clientOrderId>{client_id}</clientOrderId>
  <Order>
    <allOrNone>false</allOrNone>
    <priceType>LIMIT</priceType>
    <orderTerm>GOOD_FOR_DAY</orderTerm>
    <marketSession>REGULAR</marketSession>
    <stopPrice/>
    <limitPrice>{limit:.2f}</limitPrice>
    <Instrument>
      <Product>
        <securityType>OPTN</securityType>
        <symbol>{ticker}</symbol>
        <callPut>{option_type}</callPut>
        <expiryYear>{ey}</expiryYear>
        <expiryMonth>{em}</expiryMonth>
        <expiryDay>{ed}</expiryDay>
        <strikePrice>{strike:.1f}</strikePrice>
      </Product>
      <orderAction>{action}</orderAction>
      <quantityType>QUANTITY</quantityType>
      <quantity>{contracts}</quantity>
    </Instrument>
  </Order>"""
            if preview_id:
                base += (f"\n  <PreviewIds><previewId>{preview_id}</previewId>"
                         f"<cashMargin>CASH</cashMargin></PreviewIds>")
            return base + f"\n</{root_tag}>"

        from requests_oauthlib import OAuth1Session
        sess = OAuth1Session(
            _session["consumer_key"],
            client_secret         = _session["consumer_secret"],
            resource_owner_key    = _session["access_token"],
            resource_owner_secret = _session["access_token_secret"],
        )
        hdrs = {"Content-Type":"application/xml","Accept":"application/json"}
        base_url = f"https://api.etrade.com/v1/accounts/{account_id}/orders"

        # Fix A: preview step before place — prevents error 101
        prev = sess.post(f"{base_url}/preview",
                         data=_xml(root_tag="PreviewOrderRequest"),
                         headers=hdrs, timeout=30)
        if prev.status_code == 401:
            _session["_token_expired"] = True
            raise ValueError("Token expired")
        preview_id = None
        if prev.status_code in (200, 201):
            try:
                pr_json = prev.json()
                logger.info(f"ROLL {leg_label} preview response: {pr_json}")
                pids = pr_json.get("PreviewOrderResponse",{}).get("PreviewIds", [])
                if isinstance(pids, list) and pids:
                    preview_id = pids[0].get("previewId")
                elif isinstance(pids, dict):
                    preview_id = pids.get("previewId")
            except Exception as pe:
                logger.warning(f"ROLL {leg_label} preview parse error: {pe}")
            logger.info(f"ROLL {leg_label} preview OK — previewId={preview_id}")
        else:
            raw = prev.text[:500]
            logger.warning(f"ROLL {leg_label} preview HTTP {prev.status_code}: {raw}")

        if not preview_id:
            raise ValueError(f"{leg_label} preview did not return a previewId — cannot place order")

        resp = sess.post(f"{base_url}/place", data=_xml(preview_id),
                         headers=hdrs, timeout=30)
        if resp.status_code == 401:
            _session["_token_expired"] = True
            raise ValueError("Token expired")
        if resp.status_code not in (200, 201):
            raise ValueError(f"{leg_label} HTTP {resp.status_code}: {resp.text[:200]}")
        result   = resp.json()
        oids = result.get("PlaceOrderResponse",{}).get("OrderIds",{})
        if isinstance(oids, list) and oids:
            ids = oids[0]
        elif isinstance(oids, dict):
            ids = oids
        else:
            ids = {}
        order_id = ids.get("orderId") or ids.get("OrderId") or "?"
        logger.info(f"ROLL {leg_label}: {action} {ticker} ${strike} {expiry} "
                    f"×{contracts}c @ ${limit} → #{order_id}")
        return str(order_id), result

    try:
        # Leg 1 — BTC current position
        btc_id, _ = _place_leg("BUY_CLOSE", cur_strike, cur_expiry,
                                btc_limit, "BTC")
        # Leg 2 — STO new position
        sto_id, _ = _place_leg("SELL_OPEN", new_strike, new_expiry,
                                sto_limit, "STO")

        net_credit = round(sto_limit - btc_limit, 2)
        _append_trade_log({
            "ts":          _et_now_str(),
            "event":       "ROLL_EXECUTED",
            "ticker":      ticker,
            "btc_order_id":btc_id,
            "sto_order_id":sto_id,
            "cur_strike":  cur_strike,
            "cur_expiry":  cur_expiry,
            "new_strike":  new_strike,
            "new_expiry":  new_expiry,
            "net_credit":  net_credit,
            "contracts":   contracts,
        })
        return jsonify({
            "success":      True,
            "btc_order_id": btc_id,
            "sto_order_id": sto_id,
            "net_credit":   net_credit,
            "detail":       (f"Rolled {ticker} ${cur_strike} {cur_expiry} → "
                             f"${new_strike} {new_expiry} "
                             f"net {'credit' if net_credit>=0 else 'debit'} "
                             f"${abs(net_credit):.2f}"),
        })

    except Exception as e:
        logger.error(f"Roll failed: {e}")
        return jsonify({"success": False, "error": str(e),
                        "detail": "Roll was NOT executed. Check server log."}), 500
