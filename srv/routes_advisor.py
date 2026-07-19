"""Ollama advisor: /advisor/* (chat SSE, analyze, status, model) — extracted verbatim from server.py."""
from srv.core import (
    ADVISOR_SYSTEM, _GUARDRAILS_TEXT, _active_model, _conversation, _ollama_reachable, _session,
    _stream_ollama, app, build_advisor_prompt, json, jsonify, logger,
    request,
)


@app.route("/advisor/chat", methods=["POST"])
def advisor_chat():
    """Accept a user message, append to history, stream Ollama reply."""
    import json
    data    = request.get_json()
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message required"}), 400

    if not _ollama_reachable():
        def _err():
            yield "data: " + json.dumps({"error": True,
                "content": "⚠ Ollama is not running. Start with: ollama serve"}) + "\n\n"
            yield "data: [DONE]\n\n"
        return app.response_class(_err(), mimetype="text/event-stream",
                                  headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    # If history is empty, inject system context as first exchange
    if not _conversation:
        context = build_advisor_prompt()
        _conversation.append({"role": "system", "content": ADVISOR_SYSTEM})
        _conversation.append({"role": "user",    "content": context})
        _conversation.append({"role": "assistant",
                               "content": "I have your positions, account balance, and live screener data loaded. I'm ready to help with your wheel strategy. What would you like to analyze?"})

    # Append the new user message
    _conversation.append({"role": "user", "content": message})

    return app.response_class(
        _stream_ollama(_conversation),
        mimetype="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"}
    )


@app.route("/advisor/abort", methods=["POST"])
def advisor_abort():
    """Signal the streaming Ollama reader to stop after the current token."""
    _session["_advisor_abort"] = True
    return jsonify({"success": True, "message": "Abort signalled"})


@app.route("/advisor/chat/reset", methods=["POST"])
def advisor_chat_reset():
    """Clear conversation history and re-inject fresh context."""
    _conversation.clear()
    return jsonify({"success": True, "message": "Conversation reset"})


@app.route("/advisor/chat/history", methods=["GET"])
def advisor_chat_history():
    """Return current conversation (excluding system messages)."""
    visible = [m for m in _conversation if m["role"] != "system"]
    return jsonify({"history": visible, "length": len(visible)})


@app.route("/advisor/analyze", methods=["GET"])
def advisor_analyze():
    """Legacy single-shot analyze — now delegates to chat."""
    if not _ollama_reachable():
        def _err():
            yield "data: " + json.dumps({"error": True,
                "content": "⚠ Ollama is not running. Start with: ollama serve"}) + "\n\n"
            yield "data: [DONE]\n\n"
        return app.response_class(_err(), mimetype="text/event-stream",
                                  headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
    _conversation.clear()
    context = build_advisor_prompt()
    # Full analysis: inject complete 8-phase playbook as second system message
    # Quick chat only gets the decision tree (already in ADVISOR_SYSTEM)
    sys_msgs = [{"role": "system", "content": ADVISOR_SYSTEM}]
    if _GUARDRAILS_TEXT:
        sys_msgs.append({
            "role":    "system",
            "content": (
                "FULL WHEEL STRATEGY GUARDRAIL PLAYBOOK (Phases A-H):\n"
                "Check every applicable phase before responding. "
                "Call out any violation explicitly.\n\n"
                + _GUARDRAILS_TEXT
            ),
        })
    msgs = sys_msgs + [
        {"role": "user", "content": context + "\n\nProvide a full structured analysis of all positions."},
    ]
    return app.response_class(
        _stream_ollama(msgs),
        mimetype="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"}
    )


@app.route("/advisor/status", methods=["GET"])
def advisor_status():
    """Check if Ollama is running and model is available.
    Tries 127.0.0.1 first, then localhost (Windows IPv6 fallback)."""
    import urllib.request, json
    last_err = None
    for host in ["http://127.0.0.1:11434", "http://localhost:11434"]:
        try:
            with urllib.request.urlopen(f"{host}/api/tags", timeout=4) as r:
                data     = json.loads(r.read())
                models   = [m["name"] for m in data.get("models", [])]
                has_qwen = any("qwen2.5" in m for m in models)
                # Model quality — validated Jun 22 2026 (AI_comparison.txt)
                def _model_quality(name):
                    n = name.lower()
                    if "phi4-mini" in n or "phi4_mini" in n:
                        return "small"   # must check before "phi4" match
                    if any(x in n for x in [
                        "phi4",                         # 102-127s, best quality
                        "qwen2.5",                      # 68s, accurate and concise
                        "llama3.1:8b","mistral:7b",
                    ]):
                        return "good"
                    if any(x in n for x in [
                        "phi4-mini","phi3","phi:2",
                        "qwen2.5:3b","llama3.2","gemma2:2b",
                        "martain7r","finance-llama",    # wrong P&L attribution
                        "fin-r1","mychen76",            # structural errors on position types
                    ]):
                        return "small"
                    return "unknown"
                quality = _model_quality(_active_model["name"])
                return jsonify({
                    "ollama":        True,
                    "models":        models,
                    "qwen_ready":    has_qwen,
                    "active_model":  _active_model["name"],
                    "model_quality": quality,
                    "host":          host,
                })
        except Exception as e:
            last_err = str(e)
            continue
    return jsonify({"ollama": False, "error": last_err, "qwen_ready": False})


@app.route("/advisor/model", methods=["POST"])
def set_advisor_model():
    """Set the active Ollama model for the advisor."""
    data  = request.get_json()
    model = (data.get("model") or "").strip()
    if not model:
        return jsonify({"success": False, "error": "model name required"}), 400
    _active_model["name"] = model
    logger.info(f"Advisor model set to: {model}")
    return jsonify({"success": True, "active_model": model})


# ── Greeks AI endpoints (called by greeks.html) ──────────────────────────────

@app.route("/ai/status", methods=["GET"])
def ai_status():
    """Ollama status for greeks.html — returns {online, models, active_model}."""
    import urllib.request, json as _json
    last_err = None
    for host in ["http://127.0.0.1:11434", "http://localhost:11434"]:
        try:
            with urllib.request.urlopen(f"{host}/api/tags", timeout=4) as r:
                data   = _json.loads(r.read())
                models = [m["name"] for m in data.get("models", [])]
                return jsonify({"online": True, "models": models,
                                "active_model": _active_model["name"]})
        except Exception as e:
            last_err = str(e)
            continue
    return jsonify({"online": False, "models": [], "active_model": "",
                    "error": last_err or "Ollama not reachable"})


@app.route("/ai/greeks", methods=["POST"])
def ai_greeks():
    """Generate a plain-English trade thesis from a Greeks snapshot via Ollama."""
    import urllib.request, json as _json, time as _t
    from srv.core import OLLAMA_URL

    if not _ollama_reachable():
        return jsonify({"error": "Ollama offline — run: ollama serve"}), 503

    d = request.get_json() or {}

    ticker    = d.get("ticker", "?")
    strat     = d.get("strategy_name", d.get("direction", "") + " " + d.get("type", ""))
    S         = d.get("S", 0)
    K         = d.get("K", 0)
    DTE       = d.get("DTE", 0)
    IV        = d.get("IV", 0)
    price     = d.get("price", 0)
    breakeven = d.get("breakeven", 0)
    delta     = d.get("delta", 0)
    gamma     = d.get("gamma", 0)
    theta     = d.get("theta_dollar", 0)
    vega      = d.get("vega_dollar", 0)
    contracts = d.get("contracts", 1)
    entry     = d.get("entry_price", 0)
    model     = d.get("model") or _active_model["name"]

    system_prompt = (
        "You are a concise options trading analyst. "
        "Respond ONLY with a JSON object — no markdown, no explanation outside the JSON. "
        "Keys required: "
        "\"thesis\" (2-3 sentences: directional bias and setup rationale), "
        "\"win_condition\" (1 sentence: what must happen for max profit), "
        "\"kill_condition\" (1 sentence: specific price/event that forces a roll or close), "
        "\"greeks_verdict\" (1 sentence: which greek dominates and what it means), "
        "\"theta_power\" (1 sentence: daily decay in dollars and how many DTE to break even), "
        "\"summary\" (1 short sentence: bottom-line take), "
        "\"wheel_fit\" (one of: EXCELLENT, GOOD, FAIR, POOR)."
    )

    user_prompt = (
        f"Ticker: {ticker}  Strategy: {strat}  Contracts: {contracts}\n"
        f"Stock price: ${S}  Strike: ${K}  DTE: {DTE}  IV: {IV}%\n"
        f"Option price: ${price}  Breakeven: ${breakeven}\n"
        f"Entry credit/debit: ${entry} per contract\n"
        f"Greeks — Delta: {delta}  Gamma: {gamma}  "
        f"Theta ($/day): ${theta}  Vega ($/1% IV): ${vega}\n\n"
        "Reply with the JSON object only."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt},
    ]

    payload = _json.dumps({
        "model":    model,
        "stream":   False,
        "messages": messages,
        "options":  {"temperature": 0.2, "top_p": 0.9},
    }).encode()

    t0 = _t.time()
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw  = _json.loads(resp.read())
            text = raw.get("message", {}).get("content", "").strip()
            usage = {
                "prompt_tokens":     raw.get("prompt_eval_count", 0),
                "completion_tokens": raw.get("eval_count", 0),
                "total_tokens":      raw.get("prompt_eval_count", 0) + raw.get("eval_count", 0),
                "total_duration_ms": round(raw.get("total_duration", 0) / 1e6),
                "eval_duration_ms":  round(raw.get("eval_duration",  0) / 1e6),
            }
            # Parse JSON from model response; fall back gracefully
            try:
                # Strip possible markdown fences
                clean = text.strip()
                if clean.startswith("```"):
                    clean = clean.split("```")[1]
                    if clean.startswith("json"):
                        clean = clean[4:]
                parsed = _json.loads(clean)
            except Exception:
                parsed = {"thesis": text}

            parsed["_meta"] = {
                "system_prompt": system_prompt,
                "user_prompt":   user_prompt,
                "usage":         usage,
                "model":         model,
            }
            logger.info(f"ai/greeks {ticker} {strat} → {usage['total_tokens']} tokens "
                        f"in {round(_t.time()-t0,1)}s")
            return jsonify(parsed)
    except Exception as e:
        logger.warning(f"ai/greeks error: {e}")
        return jsonify({"error": str(e)}), 500
