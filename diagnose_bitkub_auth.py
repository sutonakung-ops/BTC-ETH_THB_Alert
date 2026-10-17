"""
สคริปต์ชั่วคราว: วินิจฉัย Bitkub error 60 ด้วย endpoint แบบอ่านอย่างเดียวเท่านั้น (ไม่มีการซื้อขาย)
ผลลัพธ์ (ไม่มี key/secret/signature) เขียนลง state/auth_diag.json
"""

import json
import os

import requests

from bitkub_trade import API_BASE, _sign, _timestamp_ms

KEY = os.environ.get("BITKUB_API_KEY", "").strip()
SECRET = os.environ.get("BITKUB_API_SECRET", "").strip()


def call(method, path, query="", body=None):
    ts = _timestamp_ms()
    body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
    payload_tail = (f"?{query}" if query else "") if method == "GET" else body_str
    sig = _sign(SECRET, ts, method, path, payload_tail)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-BTK-APIKEY": KEY,
        "X-BTK-TIMESTAMP": ts,
        "X-BTK-SIGN": sig,
    }
    url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=20)
        else:
            r = requests.post(url, headers=headers, data=body_str, timeout=20)
        try:
            data = r.json()
        except ValueError:
            data = {"non_json": r.text[:300]}
        return {"http_status": r.status_code, "response": data}
    except requests.RequestException as e:
        return {"exception": str(e)}


def main():
    diag = {
        "key_len": len(KEY),
        "secret_len": len(SECRET),
        "key_equals_secret": bool(KEY) and KEY == SECRET,
        "checks": {
            "wallet_balances_v4": call("GET", "/api/v4/wallet/balances"),
            "user_limits": call("POST", "/api/v3/user/limits"),
            "trading_credits": call("POST", "/api/v3/user/trading-credits"),
            "open_orders_btc_thb": call("GET", "/api/v3/market/my-open-orders", "sym=btc_thb"),
            "open_orders_thb_btc": call("GET", "/api/v3/market/my-open-orders", "sym=thb_btc"),
            "open_orders_BTC_THB": call("GET", "/api/v3/market/my-open-orders", "sym=BTC_THB"),
        },
    }
    # ตัดยอดเงินรายเหรียญออก เหลือแค่ THB เพื่อไม่ให้ไฟล์ใหญ่
    wb = diag["checks"]["wallet_balances_v4"].get("response")
    if isinstance(wb, dict) and isinstance(wb.get("data"), list):
        wb["data"] = [x for x in wb["data"] if str(x.get("symbol", "")).upper() in ("THB", "BTC", "ETH")]

    os.makedirs("state", exist_ok=True)
    with open("state/auth_diag.json", "w", encoding="utf-8") as f:
        json.dump(diag, f, ensure_ascii=False, indent=2)
    print(json.dumps(diag, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
