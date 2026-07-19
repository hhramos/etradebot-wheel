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
