"""OAuth flow: /auth/init, /auth/token, /auth/status, /auth/expired, /auth/reauth_ok — extracted verbatim from server.py."""
from srv.core import (
    _session, app, jsonify, logger, request,
)


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
        if accts:
            _session["account_id"] = accts[0].get("accountIdKey") or accts[0].get("accountId")

        return jsonify({
            "success":    True,
            "account_id": _session["account_id"] or "****",
            "connected":  True,
        })
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return jsonify({"success": False, "error": str(e), "hint": "Verifier codes expire quickly — try clicking Launch E-Trade login again for a fresh code"}), 502


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
