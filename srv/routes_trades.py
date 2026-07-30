"""
srv/routes_trades.py — Live E*Trade option trade history
=========================================================
/trades/sync        GET  Pull closed option orders from E*Trade → save to SQLite
/trades/report_card GET  Aggregate option_trades → per-ticker Report Card stats
"""
import datetime
import logging

from srv.core import _session, app, jsonify, logger

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── helpers ────────────────────────────────────────────────────────────────

_ACTION_MAP = {
    # (orderAction, optionType) → trade_type
    ("SELL_OPEN",  "PUT"):  "CSP_SELL",
    ("BUY_CLOSE",  "PUT"):  "CSP_BTC",
    ("BUY_TO_CLOSE","PUT"): "CSP_BTC",
    ("SELL_OPEN",  "CALL"): "CC_SELL",
    ("BUY_CLOSE",  "CALL"): "CC_BTC",
    ("BUY_TO_CLOSE","CALL"):"CC_BTC",
    ("SELL_TO_OPEN","PUT"): "CSP_SELL",
    ("SELL_TO_OPEN","CALL"):"CC_SELL",
}


def _parse_orders(orders_response: list) -> list[dict]:
    """Convert raw E*Trade order dicts to option_trades rows."""
    rows = []
    for order in orders_response:
        order_id = str(order.get("orderId", ""))
        placed_ts = order.get("orderPlacedTime") or order.get("orderValue") or ""
        # E*Trade returns epoch milliseconds for timestamps
        if isinstance(placed_ts, (int, float)) and placed_ts > 1e10:
            placed_ts = datetime.datetime.fromtimestamp(placed_ts / 1000).isoformat()
        elif not placed_ts:
            placed_ts = datetime.datetime.now().isoformat()

        for leg in order.get("OrderDetail", [{}]):
            for instrument in leg.get("Instrument", []):
                product   = instrument.get("Product", {})
                opt_type  = product.get("callPut", "").upper()   # CALL | PUT
                ticker    = product.get("symbol", "")
                strike    = product.get("strikePrice")
                expiry_yr = product.get("expiryYear")
                expiry_mo = product.get("expiryMonth")
                expiry_dy = product.get("expiryDay")
                expiry    = None
                if expiry_yr and expiry_mo and expiry_dy:
                    try:
                        expiry = f"{int(expiry_yr):04d}-{int(expiry_mo):02d}-{int(expiry_dy):02d}"
                    except (ValueError, TypeError):
                        pass

                action_raw = instrument.get("orderAction", "").upper().replace(" ", "_")
                trade_type = _ACTION_MAP.get((action_raw, opt_type))
                if not trade_type:
                    continue  # skip equity/non-option legs

                contracts = int(instrument.get("orderedQuantity") or instrument.get("filledQuantity") or 1)
                avg_price = instrument.get("averageExecutionPrice") or instrument.get("estimatedCommission")
                if avg_price is None:
                    avg_price = float(leg.get("limitPrice") or leg.get("stopPrice") or 0)
                else:
                    avg_price = float(avg_price)

                rows.append({
                    "ts":         placed_ts,
                    "order_id":   order_id + "_" + action_raw,
                    "ticker":     ticker,
                    "trade_type": trade_type,
                    "strike":     float(strike) if strike else None,
                    "expiry":     expiry,
                    "contracts":  contracts,
                    "premium":    avg_price,
                })
    return rows


# ── endpoints ──────────────────────────────────────────────────────────────

@app.route("/trades/sync", methods=["GET"])
def trades_sync():
    """Pull closed option orders from E*Trade, save new ones to SQLite."""
    if not _session.get("connected"):
        return jsonify({"error": "Not connected"}), 401

    from data import db as _db

    try:
        import pyetrade
        api = pyetrade.ETradeOrder(
            _session["consumer_key"],
            _session["consumer_secret"],
            _session["access_token"],
            _session["access_token_secret"],
            dev=False,
        )
        account_id = _session.get("account_id", "")

        # Fetch last 90 days of executed option orders
        from_date = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%m%d%Y")
        to_date   = datetime.date.today().strftime("%m%d%Y")

        resp = api.list_orders(
            account=account_id,
            resp_format="json",
            status="EXECUTED",
            fromDate=from_date,
            toDate=to_date,
            securityType="OPTN",
        )
    except Exception as e:
        logger.warning("trades/sync E*Trade call failed: %s", e)
        return jsonify({"error": str(e)}), 500

    try:
        order_list = (
            resp.get("OrdersResponse", {})
                .get("Order", [])
        )
        if isinstance(order_list, dict):
            order_list = [order_list]
    except Exception:
        order_list = []

    rows    = _parse_orders(order_list)
    synced  = 0
    for row in rows:
        if _db.upsert_option_trade(row):
            synced += 1

    last_sync = datetime.datetime.now().isoformat(timespec="seconds")
    logger.info("trades/sync: %d new fills out of %d parsed", synced, len(rows))
    return jsonify({
        "success": True,
        "synced":  synced,
        "total":   len(rows),
        "last_sync": last_sync,
    })


@app.route("/trades/report_card", methods=["GET"])
def trades_report_card():
    """Return per-ticker Report Card stats from local SQLite option_trades."""
    try:
        from data import db as _db
        result = _db.query_report_card(lookback_days=365)
        return jsonify(result)
    except Exception as e:
        logger.warning("trades/report_card error: %s", e)
        return jsonify({"error": str(e), "per_ticker": {}}), 500
