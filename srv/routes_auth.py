"""OAuth flow: /auth/init, /auth/token, /auth/status, /auth/expired, /auth/reauth_ok — extracted verbatim from server.py."""
from srv.core import (
    _CFG_PATH, _session, app, json, jsonify, logger, os, request,
)

import threading


def _load_config_safe() -> dict:
    """Load config.json without raising — returns {} on any error."""
    try:
        if os.path.exists(_CFG_PATH):
            with open(_CFG_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_preferred_account(account_id: str):
    """Persist preferred_account to config.json (best-effort)."""
    try:
        cfg = _load_config_safe() or {}
        cfg["preferred_account"] = account_id
        os.makedirs(os.path.dirname(_CFG_PATH), exist_ok=True)
        with open(_CFG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _sanitize_accounts(accts: list) -> list:
    """Return a safe, display-ready list of account dicts."""
    return [
        {
            "accountIdKey":    a.get("accountIdKey") or a.get("accountId", ""),
            "accountId":       a.get("accountId", ""),
            "accountDesc":     a.get("accountDesc") or a.get("accountName", ""),
            "accountType":     a.get("accountType", ""),
            "institutionType": a.get("institutionType", ""),
        }
        for a in accts
    ]

_FUTURES_URL = "http://127.0.0.1:5001"


def _push_to_futures():
    """Push wheel credentials to the futures server in the background (best-effort)."""
    import urllib.request, json as _json, urllib.error
    payload = _json.dumps({
        "consumer_key":        _session.get("consumer_key", ""),
        "consumer_secret":     _session.get("consumer_secret", ""),
        "access_token":        _session.get("access_token", ""),
        "access_token_secret": _session.get("access_token_secret", ""),
        "environment":         _session.get("etrade_env", "live"),
    }).encode()
    req = urllib.request.Request(
        f"{_FUTURES_URL}/auth/from_wheel",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2)
        logger.info("Credentials pushed to futures server")
    except urllib.error.URLError:
        pass  # futures server not running — silently skip


@app.route("/auth/init", methods=["POST"])
def auth_init():
    """Receive consumer key + secret from the UI. Never logged or persisted."""
    data = request.get_json()
    key    = (data.get("consumer_key")    or "").strip()
    secret = (data.get("consumer_secret") or "").strip()

    if not key or not secret:
        return jsonify({"success": False, "error": "Both consumer_key and consumer_secret required"}), 400

    _session["consumer_key"]    = key
    _session["consumer_secret"] = secret

    # Step 1 of OAuth: get a request token from E-Trade
    # pyetrade.ETradeOAuth.get_request_token() returns the auth URL string directly
    # and stores session state internally on the oauth object.
    try:
        import pyetrade
    except ImportError:
        return jsonify({"success": False, "error": "pyetrade not installed in this Python environment. Run start.bat again (it will install it), or manually run: python -m pip install pyetrade  —  Visit http://127.0.0.1:5000/debug to see which Python the server is using"}), 500

    try:
        oauth = pyetrade.ETradeOAuth(key, secret)
        auth_url = oauth.get_request_token()   # returns URL string, stores session on oauth obj
        _session["oauth_obj"] = oauth          # keep alive for the token exchange call
        import re as _re
        logger.info("Got E-Trade auth URL: %s",
                    _re.sub(r"key=[^&]+", "key=REDACTED", auth_url))
        return jsonify({"success": True, "auth_url": auth_url})
    except Exception as e:
        logger.error(f"E-Trade request token failed: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "hint": "Check your consumer key and secret at developer.etrade.com"
        }), 502


@app.route("/auth/token", methods=["POST"])
def auth_token():
    """Exchange the verifier code for an access token."""
    data     = request.get_json()
    verifier = (data.get("verifier") or "").strip()

    if not verifier:
        return jsonify({"success": False, "error": "Verifier required"}), 400
    if not _session.get("consumer_key"):
        return jsonify({"success": False, "error": "Run /auth/init first"}), 400

    oauth = _session.get("oauth_obj")
    if not oauth:
        return jsonify({"success": False, "error": "No OAuth session found — click Confirm credentials first to get a fresh auth URL"}), 400

    try:
        import pyetrade
        # get_access_token uses the session stored on the oauth object from auth/init
        tokens = oauth.get_access_token(verifier)
        _session["access_token"]        = tokens.get("oauth_token")
        _session["access_token_secret"] = tokens.get("oauth_token_secret")
        _session["connected"]           = True
        _session["_token_expired"]      = False   # clear expiry flag — new token is valid
        logger.info("Access token obtained successfully")

        # Fetch account ID
        accounts_api = pyetrade.ETradeAccounts(
            _session["consumer_key"],
            _session["consumer_secret"],
            _session["access_token"],
            _session["access_token_secret"],
            dev=False,
        )
        accounts = accounts_api.list_accounts()
        accts    = accounts.get("AccountListResponse", {}).get("Accounts", {}).get("Account", [])
        if isinstance(accts, dict):
            accts = [accts]

        sanitized = _sanitize_accounts(accts) if accts else []

        # Pick account: prefer config.json preferred_account, fall back to accts[0]
        cfg_preferred = _load_config_safe().get("preferred_account")
        chosen = None
        if cfg_preferred and accts:
            chosen = next((a for a in accts if (a.get("accountIdKey") or a.get("accountId")) == cfg_preferred), None)
        if not chosen and accts:
            chosen = accts[0]
        if chosen:
            _session["account_id"] = chosen.get("accountIdKey") or chosen.get("accountId")

        # Push credentials to futures server if it's running (best-effort, non-blocking)
        threading.Thread(target=_push_to_futures, daemon=True).start()

        return jsonify({
            "success":    True,
            "account_id": _session.get("account_id") or "****",
            "connected":  True,
            "accounts":   sanitized,
        })
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return jsonify({"success": False, "error": str(e), "hint": "Verifier codes expire quickly — try clicking Launch E-Trade login again for a fresh code"}), 502


@app.route("/auth/push_to_futures", methods=["POST"])
def push_to_futures_now():
    """On-demand synchronous credential push to futures server — called when user clicks Futures button."""
    if not _session.get("connected"):
        return jsonify({"success": False, "error": "Not connected to E*Trade"}), 400
    _push_to_futures()   # synchronous so credentials arrive before the browser opens /dashboard
    return jsonify({"success": True})


@app.route("/auth/status", methods=["GET"])
def auth_status():
    return jsonify({
        "connected":  _session["connected"],
        "account_id": _session.get("account_id"),
        "has_creds":  bool(_session.get("consumer_key")),
    })


@app.route("/auth/expired", methods=["GET"])
def auth_expired():
    """UI polls this to check if token has expired since last successful call."""
    return jsonify({
        "expired":      _session.get("_token_expired", False),
        "last_poll_et": _session.get("_last_poll_et"),
        "next_poll_et": _session.get("_next_poll_et"),
        "last_trigger": _session.get("_last_trigger"),
        "polls_today":  _session.get("_polls_today", 0),
    })


@app.route("/auth/reauth_ok", methods=["POST"])
def reauth_ok():
    """Called after successful re-auth to resume polling."""
    _session["_token_expired"] = False
    logger.info("Token expiry cleared — polling resumed")
    return jsonify({"success": True})


@app.route("/auth/accounts", methods=["GET"])
def auth_accounts():
    """Return the live account list for the picker (called after /auth/token)."""
    if not _session.get("connected"):
        return jsonify({"error": "Not connected"}), 401
    try:
        import pyetrade
        api = pyetrade.ETradeAccounts(
            _session["consumer_key"], _session["consumer_secret"],
            _session["access_token"], _session["access_token_secret"], dev=False,
        )
        raw = api.list_accounts()
        accts = raw.get("AccountListResponse", {}).get("Accounts", {}).get("Account", [])
        if isinstance(accts, dict):
            accts = [accts]
        return jsonify({"accounts": _sanitize_accounts(accts)})
    except Exception as e:
        return jsonify({"error": str(e), "accounts": []}), 500


@app.route("/auth/select_account", methods=["POST"])
def select_account():
    """Store the user's chosen account in session and persist to config.json."""
    if not _session.get("connected"):
        return jsonify({"error": "Not connected"}), 401
    data = request.get_json() or {}
    account_id = (data.get("account_id") or "").strip()
    if not account_id:
        return jsonify({"error": "account_id required"}), 400
    _session["account_id"] = account_id
    _save_preferred_account(account_id)
    return jsonify({"success": True, "account_id": account_id})
