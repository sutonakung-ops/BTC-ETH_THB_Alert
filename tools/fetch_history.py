"""ดึงราคาย้อนหลังจาก Bitkub (tradingview/history) เก็บไว้ใช้วิเคราะห์/จำลองย้อนหลัง"""
import json, os, sys, time
import requests

coins = os.environ.get("COINS", "BTC,ETH").split(",")
start = int(os.environ.get("START_TS", str(int(time.time()) - 5 * 86400)))
res = os.environ.get("RESOLUTION", "15")
out = {"resolution": res, "start": start, "fetched_at": int(time.time()), "coins": {}}
for c in coins:
    try:
        d = requests.get("https://api.bitkub.com/tradingview/history",
                         params={"symbol": f"{c}_THB", "resolution": res, "from": start, "to": int(time.time())},
                         timeout=30).json()
        out["coins"][c] = {k: d.get(k) for k in ("t", "o", "h", "l", "c", "v")}
        print(c, len(d.get("t") or []))
    except Exception as e:  # noqa: BLE001
        print(c, "error", e)
os.makedirs("analysis", exist_ok=True)
json.dump(out, open(f"analysis/history_{res}m.json", "w"))
