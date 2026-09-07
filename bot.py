import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "1000"))
STAKE_USD = float(os.getenv("STAKE_USD", "10"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "20"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "5000"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.08"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.92"))
TAKE_PROFIT = float(os.getenv("TAKE_PROFIT", "0.08"))
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.06"))

app = FastAPI(title="Polymarket V3 Real-Data Paper Bot")
lock = threading.Lock()
state = {
    "balance": STARTING_BALANCE,
    "positions": {},
    "closed": [],
    "markets": [],
    "last_scan": None,
    "error": None,
    "live_data_ok": False,
}

def now():
    return datetime.now(timezone.utc).isoformat()

def parse_json_field(value, default):
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default

def market_price_from_gamma(m):
    outcomes = parse_json_field(m.get("outcomes"), [])
    prices = parse_json_field(m.get("outcomePrices"), [])
    if not outcomes or not prices:
        return None
    for i, outcome in enumerate(outcomes):
        if str(outcome).lower() == "yes" and i < len(prices):
            try:
                return float(prices[i])
            except Exception:
                return None
    try:
        return float(prices[0])
    except Exception:
        return None

def fetch_markets():
    params = {
        "active": "true",
        "closed": "false",
        "limit": 100,
        "order": "volume24hr",
        "ascending": "false",
    }
    r = requests.get(GAMMA + "/markets", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        data = data.get("markets", [])
    result = []
    for m in data:
        p = market_price_from_gamma(m)
        if p is None or not (MIN_PRICE <= p <= MAX_PRICE):
            continue
        liquidity = float(m.get("liquidity") or 0)
        if liquidity < MIN_LIQUIDITY:
            continue
        result.append({
            "id": str(m.get("id") or m.get("conditionId") or ""),
            "question": m.get("question") or "Unknown market",
            "yes_price": p,
            "liquidity": liquidity,
            "volume24hr": float(m.get("volume24hr") or 0),
            "active": bool(m.get("active", True)),
        })
    return result

def paper_open(m, side):
    if len(state["positions"]) >= MAX_POSITIONS or state["balance"] < STAKE_USD:
        return
    key = f"{m['id']}:{side}"
    if key in state["positions"]:
        return
    entry = m["yes_price"] if side == "YES" else 1 - m["yes_price"]
    state["balance"] -= STAKE_USD
    state["positions"][key] = {
        "market_id": m["id"],
        "question": m["question"],
        "side": side,
        "entry": entry,
        "stake": STAKE_USD,
        "opened_at": now(),
    }

def paper_close(key, pos, current, reason):
    move = (current - pos["entry"]) / pos["entry"]
    pnl = pos["stake"] * move
    if reason == "TAKE_PROFIT":
        pnl = pos["stake"] * TAKE_PROFIT
    elif reason == "STOP_LOSS":
        pnl = -pos["stake"] * STOP_LOSS
    state["balance"] += pos["stake"] + pnl
    state["closed"].insert(0, {
        **pos,
        "exit": current,
        "pnl": pnl,
        "reason": reason,
        "closed_at": now(),
    })
    state["closed"] = state["closed"][:100]
    del state["positions"][key]

def strategy_cycle():
    markets = fetch_markets()
    state["markets"] = markets
    state["last_scan"] = now()
    state["live_data_ok"] = True
    state["error"] = None

    by_id = {m["id"]: m for m in markets}

    # Manage open paper positions using fresh real market prices.
    for key, pos in list(state["positions"].items()):
        m = by_id.get(pos["market_id"])
        if not m:
            continue
        current = m["yes_price"] if pos["side"] == "YES" else 1 - m["yes_price"]
        move = (current - pos["entry"]) / pos["entry"]
        if move >= TAKE_PROFIT:
            paper_close(key, pos, current, "TAKE_PROFIT")
        elif move <= -STOP_LOSS:
            paper_close(key, pos, current, "STOP_LOSS")

    # Conservative demonstration signal:
    # paper-enter only when the current implied probability is in the middle
    # range and the market has enough liquidity. This is NOT a profitability claim.
    if len(state["positions"]) < MAX_POSITIONS:
        for m in markets:
            p = m["yes_price"]
            if 0.55 <= p <= 0.70:
                paper_open(m, "YES")
            elif 0.30 <= p <= 0.45:
                paper_open(m, "NO")
            if len(state["positions"]) >= MAX_POSITIONS:
                break

def worker():
    while True:
        try:
            with lock:
                strategy_cycle()
        except Exception as e:
            state["live_data_ok"] = False
            state["error"] = repr(e)
            print("DATA ERROR:", repr(e))
        time.sleep(SCAN_SECONDS)

@app.get("/health")
def health():
    return {
        "live_data_ok": state["live_data_ok"],
        "last_scan": state["last_scan"],
        "error": state["error"],
        "markets": len(state["markets"]),
    }

@app.get("/", response_class=HTMLResponse)
def dashboard():
    with lock:
        closed = state["closed"]
        wins = sum(1 for x in closed if x["pnl"] > 0)
        losses = len(closed) - wins
        realized = sum(x["pnl"] for x in closed)
        rows = "".join(
            f"<tr><td>{x['question'][:90]}</td><td>{x['side']}</td>"
            f"<td>{x['entry']:.4f}</td><td>{x['exit']:.4f}</td>"
            f"<td>{x['pnl']:+.2f}</td><td>{x['reason']}</td></tr>"
            for x in closed[:25]
        )
        posrows = ""
        for p in state["positions"].values():
            m = next((x for x in state["markets"] if x["id"] == p["market_id"]), None)
            current = (m["yes_price"] if p["side"] == "YES" else 1 - m["yes_price"]) if m else p["entry"]
            posrows += (
                f"<tr><td>{p['question'][:90]}</td><td>{p['side']}</td>"
                f"<td>{p['entry']:.4f}</td><td>{current:.4f}</td></tr>"
            )
        status = "LIVE DATA CONNECTED" if state["live_data_ok"] else "WAITING / ERROR"
        err = state["error"] or ""
        return f"""<!doctype html><html><head><meta http-equiv="refresh" content="10">
        <title>Polymarket V3</title>
        <style>body{{font-family:Arial;margin:28px;background:#111;color:#eee}}
        .card{{display:inline-block;background:#1d1d1d;padding:14px;margin:5px;border-radius:8px}}
        table{{width:100%;border-collapse:collapse;margin-top:15px}}
        td,th{{padding:8px;border-bottom:1px solid #333;text-align:left}}
        .ok{{font-weight:bold}}</style></head><body>
        <h1>Polymarket V3 — Real-Data Paper Bot</h1>
        <p class="ok">{status}</p>
        <p>Paper only. No wallet. No API key. No real orders.</p>
        <p>Last scan: {state['last_scan'] or 'not yet'} | Markets: {len(state['markets'])}</p>
        <p>{err}</p>
        <div class="card">Balance<br><b>${state['balance']:.2f}</b></div>
        <div class="card">Realized P/L<br><b>{realized:+.2f}</b></div>
        <div class="card">Wins<br><b>{wins}</b></div>
        <div class="card">Losses<br><b>{losses}</b></div>
        <div class="card">Win rate<br><b>{(wins/len(closed)*100 if closed else 0):.1f}%</b></div>
        <h2>Open Paper Positions</h2>
        <table><tr><th>Market</th><th>Side</th><th>Entry</th><th>Current</th></tr>{posrows}</table>
        <h2>Closed Paper Trades</h2>
        <table><tr><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L</th><th>Reason</th></tr>{rows}</table>
        </body></html>"""

@app.on_event("startup")
def startup():
    threading.Thread(target=worker, daemon=True).start()
