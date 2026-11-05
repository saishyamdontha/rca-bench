#!/usr/bin/env python3
"""Record what an on-call engineer could check right now, as one JSON snapshot.

The runner takes two per trial: one just before the fault (healthy baseline)
and one in the middle of it. The agent's tools later READ these files instead
of querying a live system, so every diagnoser can be re-run on identical
evidence (reproducible, fair comparison).

Every check is read-only and time-boxed: a check that hangs is reported as
"timeout after Ns", which is itself evidence (e.g. a paused container).

  scripts/snapshot.py               # print a snapshot of the running stack
  scripts/snapshot.py --out s.json
"""
import argparse, json, os, subprocess, sys, time, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(__file__))
import faults as F

API = os.environ.get("API", "http://localhost:8080")
CHAIN_RPC = os.environ.get("CHAIN_RPC", "http://localhost:8545")
WALLET = F.WALLET
TIMEOUT = float(os.environ.get("SNAPSHOT_TIMEOUT", "5"))


def run(cmd, timeout=TIMEOUT):
    """Shell command in the target app's folder. Returns (ok, output)."""
    try:
        r = subprocess.run(cmd, shell=True, cwd=F.TARGET_DIR, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or r.stderr).strip()
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout:g}s"


# ---------------------------------------------------------------- checks ----
def container_status():
    ok, out = run("docker compose ps -a --format json")
    if not ok:
        return {"error": out[:300]}
    try:  # newer compose: one JSON object per line; older: a JSON array
        rows = json.loads(out) if out.startswith("[") else [json.loads(l) for l in out.splitlines() if l.strip()]
    except json.JSONDecodeError:
        return {"error": "unparseable docker compose ps output", "raw": out[:300]}
    res = {}
    for r in rows:
        svc = r.get("Service") or r.get("Name")
        res[svc] = {"state": r.get("State"), "health": r.get("Health") or None,
                    "status": r.get("Status"), "exit_code": r.get("ExitCode")}
    return res


DB_SQL = ("SELECT count(*) FILTER (WHERE wait_event_type = 'Lock'), "
          "count(*) FILTER (WHERE state <> 'idle'), "
          "coalesce(max(extract(epoch FROM now() - xact_start))::int, 0) "
          "FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND datname = 're2_app';")
DB_LONGEST = ("SELECT state, wait_event_type, extract(epoch FROM now() - xact_start)::int, left(query, 100) "
              "FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND datname = 're2_app' "
              "AND state <> 'idle' ORDER BY xact_start NULLS LAST LIMIT 3;")


def db_check():
    t0 = time.time()
    ok, out = run(f'docker compose exec -T db psql -U postgres -d re2_app -At -F "|" -c "{DB_SQL}"')
    res = {"reachable": ok, "latency_ms": int((time.time() - t0) * 1000)}
    if not ok:
        res["error"] = out[:300]
        return res
    try:
        waiting, active, oldest = (int(x) for x in out.split("|"))
        res.update({"sessions_waiting_on_lock": waiting, "active_sessions": active, "oldest_transaction_s": oldest})
    except ValueError:
        res["raw"] = out[:300]
    ok2, out2 = run(f'docker compose exec -T db psql -U postgres -d re2_app -At -F "|" -c "{DB_LONGEST}"')
    if ok2 and out2:
        res["active_queries"] = [dict(zip(("state", "wait", "xact_age_s", "query"), l.split("|", 3)))
                                 for l in out2.splitlines()]
    return res


def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(CHAIN_RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        d = json.load(r)
    if "error" in d:
        raise RuntimeError(d["error"].get("message", str(d["error"])))
    return d["result"]


def backend_config():
    """What the backend container was STARTED with (works even if it has exited)."""
    ok, cid = run("docker compose ps -a -q backend")
    if not ok or not cid:
        return {"error": cid[:200] or "no backend container"}
    ok, env = run(f"docker inspect --format '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {cid.splitlines()[0]}")
    if not ok:
        return {"error": env[:200]}
    cfg = dict(l.split("=", 1) for l in env.splitlines() if "=" in l)
    return {"contract_address": cfg.get("BLOCKCHAIN_CONTRACT"), "rpc": cfg.get("BLOCKCHAIN_RPC"),
            "database_url": cfg.get("DATABASE_URL"),
            # never record secrets: only whether the DB password differs from the db container's
            "db_password_matches_db_container": _db_password_matches(cfg.get("DATABASE_PASSWORD"))}


def _db_password_matches(backend_pw):
    ok, cid = run("docker compose ps -a -q db")
    if not ok or not cid:
        return None
    ok, env = run(f"docker inspect --format '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {cid.splitlines()[0]}")
    db_pw = dict(l.split("=", 1) for l in env.splitlines() if "=" in l).get("POSTGRES_PASSWORD") if ok else None
    return None if db_pw is None or backend_pw is None else backend_pw == db_pw


def chain_check(contract=None):
    res = {}
    t0 = time.time()
    try:
        res["block_number"] = int(_rpc("eth_blockNumber", []), 16)
        res["reachable"] = True
    except Exception as e:
        res.update({"reachable": False, "error": f"{type(e).__name__}: {e}"[:200]})
        return res
    res["latency_ms"] = int((time.time() - t0) * 1000)
    if contract:
        try:
            code = _rpc("eth_getCode", [contract, "latest"])
            res["configured_contract"] = contract
            res["contract_code_bytes"] = max(0, (len(code) - 2) // 2)
        except Exception as e:
            res["contract_error"] = f"{type(e).__name__}: {e}"[:200]
    return res


def wallet_balance():
    try:
        wei = int(_rpc("eth_getBalance", [WALLET, "latest"]), 16)
        return {"address": WALLET, "balance_eth": round(wei / 1e18, 6)}
    except Exception as e:
        return {"address": WALLET, "error": f"{type(e).__name__}: {e}"[:200]}


PROBES = {"properties_for_sale (DB read)": "/api/user/properties/for-sale",
          "ledger_next_id (chain read via backend)": "/ledger/next-id"}


def http_probes():
    res = {}
    for name, path in PROBES.items():
        req = urllib.request.Request(API + path, headers={"X-Request-Id": f"probe-{int(time.time())}"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                res[name] = {"status": r.status, "ms": int((time.time() - t0) * 1000)}
        except urllib.error.HTTPError as e:
            res[name] = {"status": e.code, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            res[name] = {"status": None, "ms": int((time.time() - t0) * 1000),
                         "error": f"{type(e).__name__}: {e}"[:150]}
    return res


def take():
    t0 = time.time()
    cfg = backend_config()
    snap = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "containers": container_status(),
            "backend_config": cfg,
            "db": db_check(),
            "chain": chain_check(cfg.get("contract_address")),
            "wallet": wallet_balance(),
            "http": http_probes()}
    snap["took_s"] = round(time.time() - t0, 2)
    return snap


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    s = take()
    txt = json.dumps(s, indent=2)
    if a.out:
        open(a.out, "w").write(txt)
    print(txt)
