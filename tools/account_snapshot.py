"""
สำรวจทั้งบัญชี Bitkub (อ่านอย่างเดียว ไม่ซื้อขาย) เพื่อเตรียมรวมทั้งบัญชีเป็นกองเงินบอท
- ยอดเงินบาท + ทุกเหรียญ และมูลค่าเป็นบาท
- ทุนเฉลี่ยจริงของแต่ละเหรียญ คำนวณจากประวัติการซื้อขาย (my-order-history) แบบต้นทุนเฉลี่ย
- ประวัติฝากเงินบาท (ทดสอบว่า API key อ่านได้ไหม เพื่อจับยอดเติมเงินอัตโนมัติ)
เขียนสรุปลง analysis/account_snapshot.json
"""
import json, os, sys
import requests
sys.path.insert(0, os.getcwd())
from bitkub_trade import _private_request

KEY = os.environ["BITKUB_API_KEY"].strip(); SECRET = os.environ["BITKUB_API_SECRET"].strip()
MIN_VALUE_THB = 1.0

ticker = requests.get("https://api.bitkub.com/api/market/ticker", timeout=20).json()
bal = _private_request(KEY, SECRET, "GET", "/api/v4/wallet/balances")
data = bal.get("data") if isinstance(bal, dict) else bal
rows = data if isinstance(data, list) else ([{"currency": k, **v} for k, v in data.items() if isinstance(v, dict)]
                                            if isinstance(data, dict) else [])
out = {"at": None, "thb": None, "coins": {}, "debug": {}}
for row in rows:
    cur = str(row.get("currency") or row.get("symbol") or "").upper()
    try:
        total = float(row.get("total") or 0)
    except (TypeError, ValueError):
        continue
    if cur == "THB":
        out["thb"] = {"total": total, "available": float(row.get("available") or 0)}
        continue
    t = ticker.get(f"THB_{cur}") or {}
    price = float(t.get("last") or 0)
    if total > 0:
        out["coins"][cur] = {"total": total, "price": price, "value_thb": round(total * price, 2)}

for cur, c in out["coins"].items():
    if c["value_thb"] < MIN_VALUE_THB:
        continue
    sym = f"{cur.lower()}_thb"
    trades, page = [], 1
    while page <= 10:
        r = _private_request(KEY, SECRET, "GET", "/api/v3/market/my-order-history", query=f"sym={sym}&p={page}&lmt=100&start=1500000000&end=1900000000")
        res = r.get("result") if isinstance(r, dict) else None
        if not isinstance(res, list) or not res:
            if page == 1:
                out["debug"][cur] = {k: r.get(k) for k in ("error", "message")} if isinstance(r, dict) else str(r)[:200]
            break
        trades += res
        if len(res) < 100:
            break
        page += 1
    if trades and "sample" not in out["debug"]:
        out["debug"]["sample"] = trades[0]
    trades.sort(key=lambda x: float(x.get("ts") or 0))
    coin_held, cost = 0.0, 0.0
    buys = sells = 0
    for tr in trades:
        amt, rate = float(tr.get("amount") or 0), float(tr.get("rate") or 0)
        fee, credit = float(tr.get("fee") or 0), float(tr.get("credit") or 0)
        cash_fee = max(fee - credit, 0.0)
        if tr.get("side") == "buy":  # ฝั่งซื้อ amount = เงินบาทที่จ่าย
            buys += 1
            coin_held += (amt - cash_fee) / rate if rate else 0
            cost += amt
        elif tr.get("side") == "sell" and coin_held > 0:
            sells += 1
            frac = min(amt / coin_held, 1.0)
            cost -= cost * frac
            coin_held -= min(amt, coin_held)
    c["trades"] = [{"ts": t.get("ts"), "side": t.get("side"), "rate": t.get("rate"), "amount": t.get("amount")} for t in trades]
    c.update({"history_trades": len(trades), "buys": buys, "sells": sells,
              "history_coin": round(coin_held, 10), "history_cost_thb": round(cost, 2),
              "avg_cost": round(cost / coin_held, 6) if coin_held > 0 else None,
              "unexplained_coin": round(c["total"] - coin_held, 10)})

for label, path in (("fiat_deposit", "/api/v3/fiat/deposit-history"), ("fiat_withdraw", "/api/v3/fiat/withdraw-history")):
    try:
        r = _private_request(KEY, SECRET, "POST", path + "?p=1&lmt=20", {})
        res = r.get("result") if isinstance(r, dict) else None
        out["debug"][label] = {"error": r.get("error") if isinstance(r, dict) else str(r)[:200],
                               "rows": [{k: x.get(k) for k in ("amount", "fee", "status", "time", "currency")}
                                        for x in res] if isinstance(res, list) else None,
                               "keys": sorted(res[0].keys()) if isinstance(res, list) and res else None}
    except Exception as e:  # noqa: BLE001
        out["debug"][label] = f"exception {e}"

from datetime import datetime, timezone
out["at"] = datetime.now(timezone.utc).isoformat()
os.makedirs("analysis", exist_ok=True)
json.dump(out, open("analysis/account_snapshot.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(out, ensure_ascii=False, indent=2)[:6000])
