"""
etrade_api.py — Headless E*Trade API Client
============================================
Reads OAuth tokens written by server.py (the UI) from a shared token file.
No browser required after the morning UI auth flow.

Provides all API calls the wheel bot needs:
  get_portfolio()       — open positions with greeks
  get_balance()         — account cash + buying power + NAV
  get_open_orders()     — all open orders
  get_option_chain()    — puts/calls with delta, IV, bid/ask
  get_quote()           — single or multi-ticker quote
  get_fills_since()     — orders filled after a given datetime
  preview_order()       — dry-run order (no execution)
  place_order()         — live order placement
  cancel_order()        — cancel by order ID
  place_btc_gtc()       — convenience: buy-to-close at 50% limit, GTC

Token sharing with server.py:
  The UI writes tokens to data/etrade_tokens.json on successful auth.
  This module reads that file on every API call (auto-refresh).
  If tokens are expired (401), raises TokenExpiredError — the bot
  logs it, skips execution, and queues actions for next session.
"""

import os
import json
import logging
import datetime
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "etrade_tokens.json")
BASE_URL   = "https://api.etrade.com"


class TokenExpiredError(Exception):
    """Raised when E*Trade returns 401 — tokens need re-auth via UI."""
    pass


class ETradeAPIError(Exception):
    """Raised on non-auth API errors."""
    def __init__(self, status: int, message: str):
        self.status  = status
        self.message = message
        super().__init__(f"E*Trade API error {status}: {message}")


# ── Token management ──────────────────────────────────────────────────────

class TokenStore:
    """
    Reads/writes tokens from the shared file that server.py also uses.
    Thread-safe for the bot's single-threaded monitoring loop.
    """

    def __init__(self, path: str = TOKEN_FILE):
        self.path   = path
        self._cache = None
        self._mtime = 0

    def load(self) -> dict:
        """Load tokens, re-reading file only if it changed since last load."""
        try:
            mtime = os.path.getmtime(self.path)
            if mtime != self._mtime or self._cache is None:
                with open(self.path) as f:
                    self._cache = json.load(f)
                self._mtime = mtime
                logger.debug("Tokens reloaded from %s", self.path)
            return self._cache
        except FileNotFoundError:
            raise TokenExpiredError(
                f"Token file not found: {self.path}\n"
                "Open the ETradeBot UI (start.bat), authenticate, and run the bot again."
            )
        except json.JSONDecodeError as e:
            raise TokenExpiredError(f"Token file corrupt: {e}")

    def save(self, tokens: dict):
        """Write updated tokens back to shared file."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(tokens, f, indent=2)
        self._cache = tokens
        self._mtime = os.path.getmtime(self.path)

    @property
    def consumer_key(self) -> str:
        return self.load()["consumer_key"]

    @property
    def consumer_secret(self) -> str:
        return self.load()["consumer_secret"]

    @property
    def access_token(self) -> str:
        return self.load()["access_token"]

    @property
    def access_token_secret(self) -> str:
        return self.load()["access_token_secret"]

    @property
    def account_id(self) -> str:
        return self.load()["account_id"]


# ── HTTP client with OAuth 1.0a ───────────────────────────────────────────

class ETradeClient:
    """
    Thin wrapper around requests + requests-oauthlib for E*Trade OAuth 1.0a.
    All methods return parsed Python dicts/lists.
    Raises TokenExpiredError on 401, ETradeAPIError on other failures.
    """

    def __init__(self, token_store: TokenStore = None, dry_run: bool = False):
        self.tokens   = token_store or TokenStore()
        self.dry_run  = dry_run
        self._session = None

    def _get_session(self):
        """Build OAuth1 session from current tokens. Refreshed on each call."""
        try:
            from requests_oauthlib import OAuth1Session
        except ImportError:
            raise ImportError("Run: pip install requests requests-oauthlib")

        t = self.tokens.load()
        return OAuth1Session(
            t["consumer_key"],
            client_secret    = t["consumer_secret"],
            resource_owner_key    = t["access_token"],
            resource_owner_secret = t["access_token_secret"],
        )

    def _get(self, path: str, params: dict = None) -> dict:
        session  = self._get_session()
        url      = BASE_URL + path
        response = session.get(url, params=params or {},
                               headers={"Accept": "application/json"},
                               timeout=15)
        return self._handle(response, "GET", path)

    def _post(self, path: str, body: str, content_type: str = "application/xml") -> dict:
        if self.dry_run:
            logger.info("[DRY RUN] POST %s — skipped", path)
            return {"dry_run": True, "path": path}
        session  = self._get_session()
        url      = BASE_URL + path
        response = session.post(url, data=body,
                                headers={
                                    "Content-Type": content_type,
                                    "Accept":       "application/json",
                                },
                                timeout=20)
        return self._handle(response, "POST", path)

    def _handle(self, response, method: str, path: str) -> dict:
        if response.status_code == 401:
            raise TokenExpiredError(
                "E*Trade returned 401 — tokens expired.\n"
                "Open the ETradeBot UI, re-authenticate, and restart the bot."
            )
        if response.status_code not in (200, 201):
            raise ETradeAPIError(response.status_code,
                                 response.text[:500])
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}


# ── High-level API methods ────────────────────────────────────────────────

class ETradeAPI:
    """
    High-level E*Trade API. One instance per bot session.
    All methods return clean Python structures, not raw API dicts.
    """

    def __init__(self, dry_run: bool = False, token_path: str = TOKEN_FILE):
        self.store   = TokenStore(token_path)
        self.client  = ETradeClient(self.store, dry_run=dry_run)
        self.dry_run = dry_run

    @property
    def account_id(self) -> str:
        return self.store.account_id

    # ── Account ───────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """
        Return account balance summary.
        {
            "net_value":     float,
            "buying_power":  float,
            "cash_balance":  float,
            "margin_balance": float,
        }
        """
        path = f"/v1/accounts/{self.account_id}/balance"
        raw  = self.client._get(path, {"instType": "BROKERAGE", "realTimeNAV": True})
        try:
            br       = raw.get("BalanceResponse", raw)
            computed = br.get("Computed", {})
            rtv      = computed.get("RealTimeValues", {})
            return {
                "net_value":      float(rtv.get("totalAccountValue") or
                                       computed.get("accountBalance") or 0),
                "buying_power":   float(computed.get("marginBuyingPower") or
                                       computed.get("cashBuyingPower") or 0),
                "cash_balance":   float(computed.get("cashBalance") or
                                       computed.get("netCash") or 0),
                "margin_balance": float(computed.get("marginBalance") or 0),
            }
        except Exception as e:
            logger.warning("Balance parse error: %s | raw: %s", e, str(raw)[:200])
            return {"net_value": 0, "buying_power": 0, "cash_balance": 0, "margin_balance": 0}

    # ── Portfolio ─────────────────────────────────────────────────────────

    def get_portfolio(self) -> list[dict]:
        """
        Return list of open positions.
        Each item: {
            ticker, type (CSP|CC|STOCK), strike, expiry,
            contracts, cost, current, pnl, pnl_pct,
            delta, theta, days_to_expiry
        }
        """
        path = f"/v1/accounts/{self.account_id}/portfolio"
        raw  = self.client._get(path, {"view": "QUICK", "totalsRequired": True})
        return self._parse_portfolio(raw)

    def _parse_portfolio(self, raw: dict) -> list[dict]:
        import re
        pr      = raw.get("PortfolioResponse", raw)
        accts   = pr.get("AccountPortfolio", [])
        if isinstance(accts, dict):
            accts = [accts]

        positions = []
        for acct in accts:
            raw_pos = acct.get("Position", [])
            if isinstance(raw_pos, dict):
                raw_pos = [raw_pos]
            for p in raw_pos:
                product  = p.get("Product", {})
                quick    = p.get("Quick", {}) or {}
                sec_type = product.get("securityType", "EQ")
                ticker   = product.get("symbol", "")
                qty      = float(p.get("quantity", 0) or 0)
                cost     = float(p.get("costPerShare", 0) or 0)
                current  = float(quick.get("lastTrade", cost) or cost)
                pnl      = float(p.get("totalGain", 0) or 0)
                pnl_pct  = float(p.get("totalGainPct", 0) or 0)

                if sec_type == "OPTN":
                    call_put  = product.get("callPut", "")
                    pos_type  = "CSP" if call_put == "PUT" else "CC"
                    strike    = float(product.get("strikePrice", 0) or 0)
                    ey = int(product.get("expiryYear", 0) or 0)
                    em = int(product.get("expiryMonth", 0) or 0)
                    ed = int(product.get("expiryDay", 0) or 0)
                    if ey and em and ed:
                        expiry = f"{ey}-{str(em).zfill(2)}-{str(ed).zfill(2)}"
                    else:
                        sym_desc = p.get("symbolDescription", "")
                        m = re.search(r"([A-Za-z]+)\s+(\d+)\s+'(\d+)", sym_desc)
                        if m:
                            months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                                      "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
                            mo = months.get(m.group(1), 0)
                            expiry = f"20{m.group(3)}-{str(mo).zfill(2)}-{m.group(2).zfill(2)}"
                        else:
                            expiry = ""
                    contracts = int(abs(qty)) or 1
                else:
                    pos_type  = "STOCK"
                    strike    = 0.0
                    expiry    = ""
                    contracts = int(abs(qty))

                positions.append({
                    "ticker":    ticker,
                    "type":      pos_type,
                    "strike":    strike,
                    "expiry":    expiry,
                    "contracts": contracts,
                    "cost":      round(cost, 2),
                    "current":   round(current, 2),
                    "pnl":       round(pnl, 2),
                    "pnl_pct":   round(pnl_pct, 1),
                    "delta":     None,   # populated by get_option_chain lookup
                    "theta":     None,
                })
        return positions

    # ── Orders ────────────────────────────────────────────────────────────

    def get_open_orders(self) -> list[dict]:
        """Return all open/pending orders."""
        path = f"/v1/accounts/{self.account_id}/orders"
        raw  = self.client._get(path, {"status": "OPEN"})
        return self._parse_orders(raw)

    def get_fills_since(self, since: datetime.datetime) -> list[dict]:
        """Return orders that filled after `since` datetime (ET)."""
        path = f"/v1/accounts/{self.account_id}/orders"
        raw  = self.client._get(path, {"status": "EXECUTED", "count": 100})
        orders = self._parse_orders(raw)
        return [o for o in orders
                if o.get("filled_at") and o["filled_at"] >= since]

    def _parse_orders(self, raw: dict) -> list[dict]:
        order_list = raw.get("OrdersResponse", {}).get("Order", [])
        if isinstance(order_list, dict):
            order_list = [order_list]
        result = []
        for o in order_list:
            detail = o.get("OrderDetail", [{}])
            if isinstance(detail, list):
                detail = detail[0] if detail else {}
            instr  = detail.get("Instrument", [{}])
            if isinstance(instr, list):
                instr = instr[0] if instr else {}
            product = instr.get("Product", {})

            filled_at = None
            exec_time = detail.get("executedTime") or o.get("executedTime")
            if exec_time:
                try:
                    filled_at = datetime.datetime.fromtimestamp(
                        int(exec_time) / 1000, tz=datetime.timezone.utc
                    )
                except Exception:
                    pass

            result.append({
                "order_id":    str(o.get("orderId", "")),
                "ticker":      product.get("symbol", ""),
                "action":      instr.get("orderAction", ""),
                "option_type": product.get("callPut", ""),
                "strike":      float(product.get("strikePrice", 0) or 0),
                "expiry":      product.get("expirationDate", ""),
                "contracts":   int(instr.get("orderedQuantity", 0) or 0),
                "limit_price": float(detail.get("limitPrice", 0) or 0),
                "status":      o.get("status", ""),
                "filled_at":   filled_at,
            })
        return result

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        if self.dry_run:
            logger.info("[DRY RUN] Would cancel order %s", order_id)
            return True
        path = f"/v1/accounts/{self.account_id}/orders/cancel"
        body = f"""<CancelOrderRequest>
                     <orderId>{order_id}</orderId>
                   </CancelOrderRequest>"""
        try:
            self.client._post(path, body)
            logger.info("Cancelled order %s", order_id)
            return True
        except ETradeAPIError as e:
            logger.error("Cancel order %s failed: %s", order_id, e)
            return False

    # ── Option Chain ──────────────────────────────────────────────────────

    def get_option_chain(self, ticker: str,
                         expiry_date: datetime.date | str,
                         option_type: str = "PUT") -> list[dict]:
        """
        Fetch option chain for a ticker and expiry.
        Returns list of strikes with: {
            strike, bid, ask, mid, delta, theta, gamma,
            iv, open_interest, volume, expiry
        }
        option_type: "PUT" or "CALL"
        """
        if isinstance(expiry_date, datetime.date):
            expiry_date = expiry_date.isoformat()

        # Parse expiry into components
        parts = expiry_date.split("-")
        if len(parts) == 3:
            exp_year, exp_month, exp_day = parts
        else:
            exp_year = exp_month = exp_day = ""

        path = "/v1/market/optionchains"
        params = {
            "symbol":           ticker.upper(),
            "callCount":        50 if option_type == "CALL" else 0,
            "putCount":         50 if option_type == "PUT"  else 0,
            "includeWeekly":    False,
            "skipAdjusted":     True,
            "optionCategory":   "STANDARD",
            "chainType":        option_type,
            "expiryYear":       exp_year,
            "expiryMonth":      exp_month,
            "expiryDay":        exp_day,
            "strikePriceNear":  0,
            "noOfStrikes":      30,
            "priceType":        "ALL",
        }
        raw  = self.client._get(path, params)
        return self._parse_chain(raw, option_type, expiry_date)

    def _parse_chain(self, raw: dict, option_type: str, expiry: str) -> list[dict]:
        resp   = raw.get("OptionChainResponse", {})
        pairs  = resp.get("OptionPair", [])
        if isinstance(pairs, dict):
            pairs = [pairs]

        strikes = []
        key     = "Put" if option_type == "PUT" else "Call"
        for pair in pairs:
            opt = pair.get(key, {})
            if not opt:
                continue
            bid = float(opt.get("bid", 0) or 0)
            ask = float(opt.get("ask", 0) or 0)
            mid = round((bid + ask) / 2, 2) if bid and ask else 0
            strikes.append({
                "strike":         float(opt.get("strikePrice", 0) or 0),
                "bid":            bid,
                "ask":            ask,
                "mid":            mid,
                "delta":          float(opt.get("delta", 0) or 0),
                "theta":          float(opt.get("theta", 0) or 0),
                "gamma":          float(opt.get("gamma", 0) or 0),
                "iv":             float(opt.get("impliedVolatility", 0) or 0),
                "open_interest":  int(opt.get("openInterest", 0) or 0),
                "volume":         int(opt.get("volume", 0) or 0),
                "expiry":         expiry,
                "option_type":    option_type,
            })
        return sorted(strikes, key=lambda x: x["strike"])

    # ── Quotes ────────────────────────────────────────────────────────────

    def get_quote(self, tickers: str | list[str]) -> dict[str, dict]:
        """
        Fetch quotes for one or more tickers.
        Returns {ticker: {price, bid, ask, change_pct, volume}}
        """
        if isinstance(tickers, str):
            tickers = [tickers]
        symbols = ",".join(t.upper() for t in tickers[:25])
        path    = f"/v1/market/quote/{symbols}"
        raw     = self.client._get(path, {"detailFlag": "INTRADAY"})
        return self._parse_quotes(raw)

    def _parse_quotes(self, raw: dict) -> dict[str, dict]:
        resp   = raw.get("QuoteResponse", {})
        quotes = resp.get("QuoteData", [])
        if isinstance(quotes, dict):
            quotes = [quotes]
        result = {}
        for q in quotes:
            ticker  = q.get("Product", {}).get("symbol", "")
            intraday = q.get("Intraday", {}) or {}
            result[ticker] = {
                "price":      float(q.get("All", {}).get("lastTrade", 0) or
                                    intraday.get("lastPrice", 0) or 0),
                "bid":        float(intraday.get("bid", 0) or 0),
                "ask":        float(intraday.get("ask", 0) or 0),
                "change_pct": float(intraday.get("changeClose", 0) or 0),
                "volume":     int(intraday.get("totalVolume", 0) or 0),
            }
        return result

    # ── Order placement ───────────────────────────────────────────────────

    def preview_order(self, ticker: str, action: str, option_type: str,
                      strike: float, expiry: str, contracts: int,
                      limit_price: float, tif: str = "DAY") -> dict:
        """
        Preview an option order. Returns preview ID + estimated commission.
        action: SELL_OPEN | BUY_OPEN | SELL_CLOSE | BUY_CLOSE
        """
        body = self._build_order_xml(
            ticker, action, option_type, strike, expiry,
            contracts, limit_price, tif, preview=True
        )
        path = f"/v1/accounts/{self.account_id}/orders/preview"
        raw  = self.client._post(path, body)
        preview = raw.get("PreviewOrderResponse", {})
        order   = preview.get("Order", [{}])
        if isinstance(order, list):
            order = order[0] if order else {}
        detail = order.get("OrderDetail", [{}])
        if isinstance(detail, list):
            detail = detail[0] if detail else {}
        return {
            "preview_id":   preview.get("PreviewIds", [{}])[0].get("previewId", ""),
            "commission":   float(detail.get("estimatedCommission", 0.65) or 0.65),
            "total_cost":   float(detail.get("estimatedTotalAmount", 0) or 0),
        }

    def place_order(self, ticker: str, action: str, option_type: str,
                    strike: float, expiry: str, contracts: int,
                    limit_price: float, tif: str = "DAY",
                    preview_id: str = None) -> dict:
        """
        Place an option order. Returns order_id.
        In dry_run mode logs the order but does not execute.
        """
        if self.dry_run:
            logger.info(
                "[DRY RUN] %s %s %s $%s exp %s × %dc @ $%s %s",
                action, ticker, option_type, strike, expiry,
                contracts, limit_price, tif
            )
            return {"order_id": "DRY_RUN", "dry_run": True}

        body = self._build_order_xml(
            ticker, action, option_type, strike, expiry,
            contracts, limit_price, tif,
            preview=False, preview_id=preview_id
        )
        path = f"/v1/accounts/{self.account_id}/orders/place"
        raw  = self.client._post(path, body)
        resp = raw.get("PlaceOrderResponse", {})
        ids  = resp.get("OrderIds", {}).get("orderId", "")
        logger.info(
            "Order placed: %s %s %s $%s %s × %dc → order_id=%s",
            action, ticker, option_type, strike, expiry, contracts, ids
        )
        return {"order_id": str(ids), "status": "PLACED"}

    def place_btc_gtc(self, ticker: str, option_type: str,
                      strike: float, expiry: str,
                      contracts: int, cost_basis: float) -> dict:
        """
        Convenience: place a Buy-to-Close GTC order at 50% of cost basis.
        Used for automatic profit target orders.
        """
        limit = round(cost_basis * 0.50, 2)
        limit = max(limit, 0.05)   # minimum $0.05
        logger.info(
            "Placing BTC GTC: %s %s $%s exp %s × %dc @ $%s (50%% of $%s)",
            ticker, option_type, strike, expiry, contracts, limit, cost_basis
        )
        return self.place_order(
            ticker     = ticker,
            action     = "BUY_CLOSE",
            option_type= option_type,
            strike     = strike,
            expiry     = expiry,
            contracts  = contracts,
            limit_price= limit,
            tif        = "GTC",
        )

    def _build_order_xml(self, ticker: str, action: str, option_type: str,
                          strike: float, expiry: str, contracts: int,
                          limit_price: float, tif: str,
                          preview: bool = False,
                          preview_id: str = None) -> str:
        """Build the XML order body E*Trade expects."""
        # Parse expiry date
        parts  = expiry.split("-")
        exp_year, exp_month, exp_day = (parts + ["","",""])[:3]

        preview_block = ""
        if not preview and preview_id:
            preview_block = f"<PreviewIds><previewId>{preview_id}</previewId></PreviewIds>"

        tag = "PreviewOrderRequest" if preview else "PlaceOrderRequest"
        return f"""<?xml version="1.0" encoding="utf-8"?>
<{tag}>
  <orderType>OPTN</orderType>
  <clientOrderId>{self._client_order_id()}</clientOrderId>
  {preview_block}
  <Order>
    <allOrNone>false</allOrNone>
    <priceType>LIMIT</priceType>
    <orderTerm>{tif}</orderTerm>
    <marketSession>REGULAR</marketSession>
    <stopPrice/>
    <limitPrice>{limit_price}</limitPrice>
    <Instrument>
      <Product>
        <securityType>OPTN</securityType>
        <symbol>{ticker.upper()}</symbol>
        <callPut>{option_type.upper()}</callPut>
        <expiryYear>{exp_year}</expiryYear>
        <expiryMonth>{exp_month}</expiryMonth>
        <expiryDay>{exp_day}</expiryDay>
        <strikePrice>{strike}</strikePrice>
      </Product>
      <orderAction>{action}</orderAction>
      <quantityType>QUANTITY</quantityType>
      <quantity>{contracts}</quantity>
    </Instrument>
  </Order>
</{tag}>"""

    @staticmethod
    def _client_order_id() -> str:
        """Generate a unique client order ID."""
        import time
        return f"WB{int(time.time() * 1000) % 10_000_000}"


# ── Token file writer (called by server.py after OAuth) ──────────────────

def write_tokens_from_server_session(session_data: dict,
                                      path: str = TOKEN_FILE):
    """
    Write tokens to the shared file after server.py completes OAuth.
    server.py calls this at the end of /auth/token.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tokens = {
        "consumer_key":          session_data.get("consumer_key", ""),
        "consumer_secret":       session_data.get("consumer_secret", ""),
        "access_token":          session_data.get("access_token", ""),
        "access_token_secret":   session_data.get("access_token_secret", ""),
        "account_id":            session_data.get("account_id", ""),
        "written_at":            datetime.datetime.utcnow().isoformat(),
    }
    with open(path, "w") as f:
        json.dump(tokens, f, indent=2)
    logger.info("Tokens written to %s", path)


# ── Self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    api = ETradeAPI(dry_run=True)
    print("\n─── ETradeAPI Self-test (dry_run=True) ───")
    try:
        bal = api.get_balance()
        print(f"Balance: {bal}")
    except TokenExpiredError as e:
        print(f"Tokens not found (expected in test): {e}")
        sys.exit(0)

    port = api.get_portfolio()
    print(f"Portfolio: {len(port)} positions")

    orders = api.get_open_orders()
    print(f"Open orders: {len(orders)}")

    # Test BTC order build (dry run)
    result = api.place_btc_gtc("SOFI","PUT", 15.0, "2026-07-17", 1, 0.57)
    print(f"BTC GTC (dry): {result}")
    print("─── Self-test complete ───\n")
