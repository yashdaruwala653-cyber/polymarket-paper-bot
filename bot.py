import json, os, sqlite3, threading, time
from datetime import datetime, timezone
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

GAMMA = "https://gamma-api.polymarket.com"
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "1000"))
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "15"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "8"))
MAX_TRADES_HOUR = int(os.getenv("MAX_TRADES_HOUR", "12"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "45"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "5000"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.05"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.95"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
MAX_STAKE = float(os.getenv("MAX_STAKE", "50"))
MIN_STAKE = float(os.getenv("MIN_STAKE", "5"))
SL_PCT = float(os.getenv("SL_PCT", "0.06"))
TP_PCT = float(os.getenv("TP_PCT", "0.12"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.68"))
HISTORY_LEN = int(os.getenv("HISTORY_LEN", "30"))
LEARN_EVERY = int(os.getenv("LEARN_EVERY", "20"))
DB_PATH = os.getenv("DB_PATH", "paper_bot.db")

app = FastAPI(title="Polymarket V4 Adaptive Real-Data Paper Bot")
lock = threading.Lock()
state = {
    "balance": STARTING_BALANCE, "positions": {}, "closed": [], "markets": [],
    "last_scan": None, "error": None, "live_data_ok": False, "cycle": 0,
    "entry_threshold": MIN_CONFIDENCE, "momentum_threshold": 0.004,
    "tp_pct": TP_PCT, "sl_pct": SL_PCT, "last_trade_at": 0.0,
    "trades_opened": 0, "last_learning": None
}
price_history = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def parse(v, default):
    if isinstance(v, list): return v
    try: return json.loads(v)
    except Exception: return default


def db():
    return sqlite3.connect(DB_PATH, timeout=20)


def db_init():
    con = db()
    con.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT, question TEXT, side TEXT, entry REAL, exit REAL, stake REAL, pnl REAL, confidence REAL, momentum REAL, reason TEXT, opened_at TEXT, closed_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit(); con.close()


def load_state():
    con = db()
    rows = con.execute("SELECT market_id,question,side,entry,exit,stake,pnl,confidence,momentum,reason,opened_at,closed_at FROM trades ORDER BY id DESC LIMIT 500").fetchall()
    meta = dict(con.execute("SELECT key,value FROM meta").fetchall())
    con.close()
    state["closed"] = []
    for r in rows:
        state["closed"].append({"market_id":r[0],"question":r[1],"side":r[2],"entry":r[3],"exit":r[4],"stake":r[5],"pnl":r[6],"confidence":r[7],"momentum":r[8],"reason":r[9],"opened_at":r[10],"closed_at":r[11]})
    state["balance"] = float(meta.get("balance", STARTING_BALANCE))
    state["entry_threshold"] = float(meta.get("entry_threshold", MIN_CONFIDENCE))
    state["momentum_threshold"] = float(meta.get("momentum_threshold", 0.004))
    state["trades_opened"] = int(meta.get("trades_opened", 0))


def save_meta():
    con = db()
    values = {
        "balance": state["balance"], "entry_threshold": state["entry_threshold"],
        "momentum_threshold": state["momentum_threshold"], "trades_opened": state["trades_opened"]
    }
    con.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", [(k,str(v)) for k,v in values.items()])
    con.commit(); con.close()


def fetch_markets():
    r = requests.get(GAMMA + "/markets", params={"active":"true","closed":"false","limit":100,"order":"volume24hr","ascending":"false"}, timeout=20)
    r.raise_for_status()
    data = r.json(); data = data.get("markets", []) if isinstance(data, dict) else data
    out = []
    for m in data:
        outcomes, prices = parse(m.get("outcomes"), []), parse(m.get("outcomePrices"), [])
        if not outcomes or not prices: continue
        p = None
        for i, o in enumerate(outcomes):
            if i < len(prices) and str(o).lower() == "yes":
                try: p = float(prices[i])
                except: pass
                break
        if p is None:
            try: p = float(prices[0])
            except: continue
        liq = float(m.get("liquidity") or 0); vol = float(m.get("volume24hr") or 0)
        if not (MIN_PRICE <= p <= MAX_PRICE) or liq < MIN_LIQUIDITY: continue
        midkey = str(m.get("id") or m.get("conditionId") or "")
        if not midkey: continue
        out.append({"id":midkey,"question":m.get("question") or "Unknown","yes_price":p,"liquidity":liq,"volume24hr":vol})
    return out


def score_market(m):
    key, p = m["id"], m["yes_price"]
    h = price_history.setdefault(key, [])
    h.append((time.time(), p)); del h[:-HISTORY_LEN]
    if len(h) < 5: return None
    old = h[0][1]
    short_base = h[-4][1]
    momentum = (p-old)/old if old else 0
    short = (p-short_base)/short_base if short_base else 0
    direction = 1 if momentum >= 0 else -1
    # Signal combines multi-window momentum, liquidity, and distance from the 50/50 area.
    mom_score = min(1.0, abs(momentum)/0.012)
    short_score = min(1.0, abs(short)/0.006)
    liq_score = min(1.0, m["liquidity"]/50000)
    price_edge = min(1.0, abs(p-0.5)/0.30)
    confidence = 0.50*mom_score + 0.20*short_score + 0.15*liq_score + 0.15*price_edge
    if abs(momentum) > 0.025: confidence *= 0.70
    side = "YES" if direction > 0 else "NO"
    side_price = p if side == "YES" else 1-p
    return {"confidence":min(0.99, confidence),"momentum":momentum,"short":short,"side":side,"price":side_price}


def stake_for(conf):
    risk_budget = state["balance"] * RISK_PER_TRADE * max(1.0, conf/max(state["entry_threshold"],0.01))
    stake = risk_budget / max(state["sl_pct"],0.001)
    return round(min(MAX_STAKE, max(MIN_STAKE, stake), state["balance"]), 2)


def trades_last_hour():
    cutoff = time.time()-3600
    count = 0
    for x in state["closed"]:
        try:
            if datetime.fromisoformat(x["opened_at"]).timestamp() > cutoff: count += 1
        except: pass
    for x in state["positions"].values():
        try:
            if datetime.fromisoformat(x["opened_at"]).timestamp() > cutoff: count += 1
        except: pass
    return count


def paper_open(m, sc):
    if len(state["positions"]) >= MAX_POSITIONS or trades_last_hour() >= MAX_TRADES_HOUR: return False
    if time.time()-state["last_trade_at"] < COOLDOWN_SECONDS: return False
    key = f"{m['id']}:{sc['side']}"
    if key in state["positions"]: return False
    if sc["confidence"] < state["entry_threshold"] or abs(sc["momentum"]) < state["momentum_threshold"]: return False
    stake = stake_for(sc["confidence"])
    if stake < MIN_STAKE or state["balance"] < stake: return False
    state["balance"] -= stake
    state["positions"][key] = {"market_id":m["id"],"question":m["question"],"side":sc["side"],"entry":sc["price"],"stake":stake,"opened_at":now(),"confidence":sc["confidence"],"momentum":sc["momentum"]}
    state["last_trade_at"] = time.time(); state["trades_opened"] += 1
    save_meta(); return True


def close_trade(key, pos, current, reason):
    move = (current-pos["entry"])/pos["entry"] if pos["entry"] else 0
    pnl = pos["stake"]*move
    if reason == "TAKE_PROFIT": pnl = pos["stake"]*state["tp_pct"]
    elif reason == "STOP_LOSS": pnl = -pos["stake"]*state["sl_pct"]
    state["balance"] += pos["stake"] + pnl
    rec = {**pos,"exit":current,"pnl":pnl,"reason":reason,"closed_at":now()}
    state["closed"].insert(0,rec); state["closed"] = state["closed"][:500]
    con = db()
    con.execute("INSERT INTO trades(market_id,question,side,entry,exit,stake,pnl,confidence,momentum,reason,opened_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pos["market_id"],pos["question"],pos["side"],pos["entry"],current,pos["stake"],pnl,pos["confidence"],pos["momentum"],reason,pos["opened_at"],rec["closed_at"]))
    con.commit(); con.close(); del state["positions"][key]; save_meta()


def learn():
    n = len(state["closed"])
    if n == 0 or n % LEARN_EVERY != 0: return
    recent = state["closed"][:LEARN_EVERY]
    wr = sum(x["pnl"] > 0 for x in recent)/len(recent)
    avg = sum(x["pnl"] for x in recent)/len(recent)
    if wr < 0.45 or avg < 0:
        state["entry_threshold"] = min(0.88, state["entry_threshold"]+0.03)
        state["momentum_threshold"] = min(0.03, state["momentum_threshold"]*1.15)
    elif wr > 0.60 and avg > 0:
        state["entry_threshold"] = max(0.62, state["entry_threshold"]-0.015)
        state["momentum_threshold"] = max(0.0015, state["momentum_threshold"]*0.95)
    state["entry_threshold"] = round(state["entry_threshold"],3)
    state["momentum_threshold"] = round(state["momentum_threshold"],5)
    state["last_learning"] = now(); save_meta()


def cycle():
    markets = fetch_markets(); state["markets"] = markets; state["last_scan"] = now(); state["live_data_ok"] = True; state["error"] = None; state["cycle"] += 1
    by = {m["id"]:m for m in markets}
    for key,pos in list(state["positions"].items()):
        m = by.get(pos["market_id"])
        if not m: continue
        cur = m["yes_price"] if pos["side"] == "YES" else 1-m["yes_price"]
        move = (cur-pos["entry"])/pos["entry"] if pos["entry"] else 0
        if move >= state["tp_pct"]: close_trade(key,pos,cur,"TAKE_PROFIT")
        elif move <= -state["sl_pct"]: close_trade(key,pos,cur,"STOP_LOSS")
    learn()
    candidates=[]
    for m in markets[:60]:
        sc=score_market(m)
        if sc and sc["confidence"]>=state["entry_threshold"] and abs(sc["momentum"])>=state["momentum_threshold"]:
            rank=0.75*sc["confidence"]+0.15*min(1,m["liquidity"]/50000)+0.10*min(1,m["volume24hr"]/100000)
            candidates.append((rank,m,sc))
    candidates.sort(key=lambda x:x[0],reverse=True)
    for _,m,sc in candidates:
        if len(state["positions"])>=MAX_POSITIONS: break
        paper_open(m,sc)


def worker():
    db_init(); load_state()
    while True:
        try:
            with lock: cycle()
        except Exception as e:
            state["live_data_ok"] = False; state["error"] = repr(e); print("DATA ERROR:",repr(e))
        time.sleep(SCAN_SECONDS)


@app.get('/health')
def health():
    return {"live_data_ok":state["live_data_ok"],"last_scan":state["last_scan"],"error":state["error"],"markets":len(state["markets"]),"open_positions":len(state["positions"]),"cycle":state["cycle"]}


@app.get('/',response_class=HTMLResponse)
def dashboard():
    with lock:
        closed=state["closed"]; wins=sum(x["pnl"]>0 for x in closed); losses=len(closed)-wins; realized=sum(x["pnl"] for x in closed)
        rows=''.join(f"<tr><td>{p['question'][:90]}</td><td>{p['side']}</td><td>{p['entry']:.4f}</td><td>{(next((m['yes_price'] if p['side']=='YES' else 1-m['yes_price'] for m in state['markets'] if m['id']==p['market_id']),p['entry'])):.4f}</td><td>${p['stake']:.2f}</td><td>{p['confidence']:.2f}</td></tr>" for p in state['positions'].values())
        hist=''.join(f"<tr><td>{x['question'][:70]}</td><td>{x['side']}</td><td>{x['entry']:.4f}</td><td>{x['exit']:.4f}</td><td>{x['pnl']:+.2f}</td><td>{x['reason']}</td></tr>" for x in closed[:40])
        status='LIVE DATA CONNECTED' if state['live_data_ok'] else 'WAITING / ERROR'
        return f'''<!doctype html><html><head><meta http-equiv="refresh" content="10"><title>Polymarket V4</title><style>body{{font-family:Arial;margin:28px;background:#111;color:#eee}}.card{{display:inline-block;background:#1d1d1d;padding:14px;margin:5px;border-radius:8px}}table{{width:100%;border-collapse:collapse;margin-top:15px}}td,th{{padding:7px;border-bottom:1px solid #333;text-align:left}}small{{color:#aaa}}.ok{{font-weight:bold}}</style></head><body><h1>Polymarket V4 — Adaptive Real-Data Paper Bot</h1><p class="ok">{status}</p><p>Paper only. No wallet. No API key. No real orders.</p><p>Last scan: {state['last_scan'] or 'not yet'} | Markets: {len(state['markets'])} | Cycle: {state['cycle']}</p><p>{state['error'] or ''}</p><div class="card">Balance<br><b>${state['balance']:.2f}</b></div><div class="card">Realized P/L<br><b>{realized:+.2f}</b></div><div class="card">Wins<br><b>{wins}</b></div><div class="card">Losses<br><b>{losses}</b></div><div class="card">Win rate<br><b>{(wins/len(closed)*100 if closed else 0):.1f}%</b></div><div class="card">Open<br><b>{len(state['positions'])}/{MAX_POSITIONS}</b></div><h2>Adaptive Parameters</h2><p>Confidence: <b>{state['entry_threshold']:.3f}</b> | Momentum: <b>{state['momentum_threshold']:.4f}</b> | SL: <b>{state['sl_pct']*100:.1f}%</b> | TP: <b>{state['tp_pct']*100:.1f}%</b> | Max trades/hour: <b>{MAX_TRADES_HOUR}</b> | Cooldown: <b>{COOLDOWN_SECONDS}s</b></p><h2>Open Paper Positions</h2><table><tr><th>Market</th><th>Side</th><th>Entry</th><th>Current</th><th>Stake</th><th>Conf.</th></tr>{rows}</table><h2>Closed Paper Trades</h2><table><tr><th>Market</th><th>Side</th><th>Entry</th><th>Exit</th><th>P/L</th><th>Reason</th></tr>{hist}</table></body></html>'''


@app.on_event('startup')
def startup():
    threading.Thread(target=worker, daemon=True).start()
