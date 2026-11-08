"""Diagnoser (c): LLM agent with read-only investigation tools.

Starts from EXACTLY the same input as diagnoser (b) (error lines, silent and
restarted services), then may call tools before answering. Tools read the
snapshots recorded during the trial (snapshot_before.json = healthy baseline,
snapshot_during.json = while the fault was active), so every run sees
identical evidence and results are reproducible.

  RCA_MODEL=qwen3:8b RCA_THINK=1 python3 -m rca.evaluate --diagnoser agent --trials trials_v2
"""
import json, os, re, urllib.request

from rca import llm as L

MODEL = os.environ.get("RCA_MODEL", "qwen3:8b")
HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
THINK = os.environ.get("RCA_THINK", "1") == "1"
MAX_TOOL_CALLS = int(os.environ.get("RCA_MAX_TOOL_CALLS", "6"))
TIMEOUT = int(os.environ.get("RCA_TIMEOUT", "600"))

SYSTEM = L.SYSTEM.replace(
    "Answer with JSON only:",
    """You can investigate before answering. Tools show the system's state BEFORE the incident
(healthy baseline) and NOW (during the incident). Compare them. Check the components the errors
point at, but also the ones they don't: the root cause is often not where the symptoms appear.
Call at most a few tools, then answer.

Evidence may quote the log lines OR the tool results you received, copied exactly.

When you give your final answer, answer with JSON only:""")


# ------------------------------------------------------------------ tools ---
def _both(inc, key):
    before = inc.snapshots.get("before", {}).get(key, "no snapshot recorded")
    now = inc.snapshots.get("during", {}).get(key, "no snapshot recorded")
    return {"before_incident": before, "now": now}


def _grep_logs(inc, pattern, limit=10):
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        return {"error": f"bad regex: {e}"}
    hits = []
    for r in inc.window:
        text = inc.text(r)
        if rx.search(text):
            hits.append(f"[{r.get('service')}] {r.get('level')} {text[:300]}")
            if len(hits) >= int(limit):
                break
    return {"pattern": pattern, "matches": hits, "note": "first matches only" if len(hits) >= int(limit) else ""}


TOOLS = {
    "get_containers": (lambda inc, **_: _both(inc, "containers"),
                       "State of every container (running/paused/exited, health)."),
    "check_database": (lambda inc, **_: _both(inc, "db"),
                       "PostgreSQL: reachable?, active sessions, sessions waiting on locks, oldest open transaction, active queries."),
    "check_chain": (lambda inc, **_: _both(inc, "chain"),
                    "Blockchain node: reachable?, block height, and contract code size at the backend's configured contract address."),
    "check_wallet": (lambda inc, **_: _both(inc, "wallet"),
                     "ETH balance of the wallet the backend uses to pay for transactions."),
    "check_backend_config": (lambda inc, **_: _both(inc, "backend_config"),
                             "Configuration the backend container was started with: contract address, RPC URL, DB URL, whether its DB password matches the database's."),
    "probe_endpoints": (lambda inc, **_: _both(inc, "http"),
                        "HTTP status and latency of a DB-read endpoint and a chain-read endpoint of the backend."),
    "grep_logs": (lambda inc, pattern=".", limit=10, **_: _grep_logs(inc, pattern, limit),
                  "Search ALL log lines (any level) during the incident with a regex. Returns up to `limit` matching lines."),
}


def _tool_schema():
    out = []
    for name, (_, desc) in TOOLS.items():
        params = {"type": "object", "properties": {}, "required": []}
        if name == "grep_logs":
            params = {"type": "object",
                      "properties": {"pattern": {"type": "string", "description": "regex, case-insensitive"},
                                     "limit": {"type": "integer", "description": "max lines (default 10)"}},
                      "required": ["pattern"]}
        out.append({"type": "function", "function": {"name": name, "description": desc, "parameters": params}})
    return out


# ------------------------------------------------------------------- loop ---
def _chat(messages, tools=None, fmt=None):
    payload = {"model": MODEL, "stream": False, "think": THINK, "messages": messages,
               "options": {"temperature": 0, "num_ctx": L.NUM_CTX}}
    if tools:
        payload["tools"] = tools
    if fmt:
        payload["format"] = fmt
    req = urllib.request.Request(f"{HOST}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _parse_final(content):
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            return {}


def diagnose(inc):
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": L.build_prompt(inc)}]
    schema, calls, tokens = _tool_schema(), [], {"prompt": 0, "output": 0}

    def count(resp):
        tokens["prompt"] += resp.get("prompt_eval_count") or 0
        tokens["output"] += resp.get("eval_count") or 0

    # Investigation phase: let the model call tools until it stops or hits the budget.
    while len(calls) < MAX_TOOL_CALLS:
        resp = _chat(messages, tools=schema)
        count(resp)
        msg = resp.get("message", {})
        tcs = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tcs})
        if not tcs:
            break
        for tc in tcs[: MAX_TOOL_CALLS - len(calls)]:
            fn = tc.get("function", {})
            name, args = fn.get("name", ""), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if name in TOOLS:
                try:
                    result = TOOLS[name][0](inc, **args)
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
            else:
                result = {"error": f"unknown tool {name!r}; available: {list(TOOLS)}"}
            text = json.dumps(result, indent=1)[:4000]
            calls.append({"name": name, "args": args, "result": text})
            messages.append({"role": "tool", "tool_name": name, "content": text})

    # Final phase: one structured answer, no more tools.
    messages.append({"role": "user", "content": "Give your final diagnosis now as JSON (component, root_cause, evidence)."})
    resp = _chat(messages, fmt=L.SCHEMA)
    count(resp)
    out = _parse_final(resp.get("message", {}).get("content", ""))

    comp = str(out.get("component", "")).strip().lower()
    ev = out.get("evidence", [])
    if not isinstance(ev, list):
        ev = [str(ev)]
    return {"component": comp if comp in L.COMPONENTS else f"invalid:{comp}",
            "root_cause": str(out.get("root_cause", ""))[:500],
            "evidence": [str(e) for e in ev][:5],
            "model": MODEL, "think": THINK,
            "tool_calls": calls, "n_tool_calls": len(calls), "tokens": tokens}
