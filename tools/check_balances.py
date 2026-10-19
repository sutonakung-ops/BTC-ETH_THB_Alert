"""
ตรวจยอดเหรียญจริงใน Bitkub เทียบกับที่บอทบันทึกไว้ (อ่านอย่างเดียว ไม่ซื้อขาย)
เขียนผลเฉพาะเหรียญที่บอทเกี่ยวข้อง (ไม่รวมเงินบาท / BTC / ETH ที่ผู้ใช้ถือเองด้วย) เพราะ repo เป็น public
"""
import json, os, sys
sys.path.insert(0, os.getcwd())
from bitkub_trade import _private_request

KEY = os.environ["BITKUB_API_KEY"].strip(); SECRET = os.environ["BITKUB_API_SECRET"].strip()
SKIP = {"THB", "BTC", "ETH"}
state = json.load(open("state/dca_state.json"))
coins = set(state["holdings"]) | {e["name"] for e in state.get("sell_log", [])} | {e["name"] for e in state.get("log", [])}
coins -= SKIP

out = {"note": "ยอดจริงใน Bitkub เทียบยอดที่บอทบันทึก (เฉพาะเหรียญที่บอทเกี่ยวข้อง)", "coins": {}, "raw_status": {}}
for label, path in (("balances", "/api/v4/wallet/balances"), ("assets", "/api/v4/wallet/assets")):
    try:
        r = _private_request(KEY, SECRET, "GET", path)
    except Exception as e:  # noqa: BLE001
        out["raw_status"][label] = f"error {e}"; continue
    out["raw_status"][label] = {"code": r.get("code"), "message": r.get("message"), "rows": len(r.get("data") or [])}
    for row in r.get("data") or []:
        cur = str(row.get("currency") or row.get("symbol") or "").upper()
        if cur in coins:
            out["coins"].setdefault(cur, {})[label] = {k: row.get(k) for k in row if k != "currency"}
for c in sorted(coins):
    rec = (state["holdings"].get(c) or {}).get("total_coin", 0.0)
    d = out["coins"].setdefault(c, {}); d["recorded_coin"] = rec
    tot = None
    try:
        tot = float((d.get("balances") or {}).get("total"))
    except (TypeError, ValueError):
        pass
    d["wallet_total"] = tot
    if tot is not None:
        d["diff_coin"] = tot - rec
        d["diff_pct_of_recorded"] = round((tot / rec - 1) * 100, 4) if rec else None
os.makedirs("analysis", exist_ok=True)
json.dump(out, open("analysis/balance_check.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(out, ensure_ascii=False, indent=2)[:3000])
