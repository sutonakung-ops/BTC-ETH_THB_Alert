"""
ตัวเชื่อมต่อ Bitkub Private API (ฝั่งซื้อ-ขายจริง)
อ้างอิงวิธี sign request จากเอกสารทางการ: https://api.bitkub.com/docs/authentication

ไฟล์นี้ไม่เก็บ API key/secret เอง — รับมาจาก environment variables เสมอ
(ตั้งผ่าน GitHub Secrets: BITKUB_API_KEY, BITKUB_API_SECRET)
"""

import hashlib
import hmac
import json
import time

import requests

API_BASE = "https://api.bitkub.com"


class BitkubAPIError(Exception):
    pass


def _timestamp_ms():
    """
    ใช้เวลาจากเซิร์ฟเวอร์ Bitkub เอง (GET /api/v3/servertime) แทนนาฬิกาเครื่องที่รันโค้ด (GitHub Actions runner)
    เพราะ error code 6 "Missing or invalid signature" ของ Bitkub เกิดได้จากนาฬิกาเหลื่อมกันเกิน tolerance
    ที่เขากำหนดไว้ (ไม่ประกาศตัวเลขชัดเจน) แม้จะคลาดกันแค่ไม่กี่วินาที — ใช้เวลาเซิร์ฟเวอร์ตรงๆ ตัดปัญหานี้ทิ้งไปเลย
    ถ้าดึงเวลาเซิร์ฟเวอร์ไม่สำเร็จ (เน็ตมีปัญหาชั่วคราว) fallback ไปใช้นาฬิกาเครื่องแทน ดีกว่าหยุดทำงานไปเลย
    """
    try:
        resp = requests.get(f"{API_BASE}/api/v3/servertime", timeout=10)
        return str(int(resp.json()))
    except (requests.RequestException, ValueError, TypeError):
        return str(int(time.time() * 1000))


def _sign(api_secret, timestamp, method, path, body=""):
    payload = f"{timestamp}{method}{path}{body}"
    return hmac.new(
        api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _private_request(api_key, api_secret, method, path, body_dict=None, query=""):
    if not api_key or not api_secret:
        raise BitkubAPIError("ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET")

    timestamp = _timestamp_ms()
    body = json.dumps(body_dict, separators=(",", ":")) if body_dict is not None else ""
    # signature = {timestamp}{METHOD}{/api/v3/path}{?query หรือ body} ตาม rest-v3.md (path มี /v3/ ด้วย)
    query_part = f"?{query}" if query else ""
    tail = query_part if method.upper() == "GET" else body
    signature = _sign(api_secret, timestamp, method.upper(), path, tail)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-BTK-APIKEY": api_key,
        "X-BTK-TIMESTAMP": timestamp,
        "X-BTK-SIGN": signature,
    }

    url = f"{API_BASE}{path}{query_part}"
    if method.upper() == "GET":
        response = requests.get(url, headers=headers, timeout=20)
    else:
        response = requests.post(url, headers=headers, data=body, timeout=20)

    try:
        data = response.json()
    except ValueError:
        raise BitkubAPIError(f"API ตอบกลับไม่ใช่ JSON: {response.text[:200]}")

    return data


def place_bid(api_key, api_secret, sym, amt_thb, rat=0, typ="market", client_id=None):
    """
    สั่งซื้อ (จ่ายเป็นเงินบาท). sym เช่น 'THB_BTC'.
    typ='market' -> ให้ rat=0 (ซื้อที่ราคาตลาดปัจจุบันทันที)
    """
    body = {"sym": sym, "amt": amt_thb, "rat": rat, "typ": typ}
    if client_id:
        body["client_id"] = client_id
    return _private_request(api_key, api_secret, "POST", "/api/v3/market/place-bid", body)


def place_ask(api_key, api_secret, sym, amt_coin, rat=0, typ="market", client_id=None):
    """
    สั่งขาย (จ่ายเป็นจำนวนเหรียญ). sym เช่น 'THB_BTC'.
    typ='market' -> ให้ rat=0 (ขายที่ราคาตลาดปัจจุบันทันที)
    """
    body = {"sym": sym, "amt": amt_coin, "rat": rat, "typ": typ}
    if client_id:
        body["client_id"] = client_id
    return _private_request(api_key, api_secret, "POST", "/api/v3/market/place-ask", body)


def get_balances(api_key, api_secret):
    return _private_request(api_key, api_secret, "GET", "/api/v4/wallet/balances")


def floor_amount(x, decimals=8):
    """ปัดเศษลง (ไม่ปัดขึ้น) — ปัดขึ้นทำให้สั่งขายเกินยอดจริงแค่ 0.00000001 ก็โดนปฏิเสธว่าเหรียญไม่พอ (error 18)"""
    f = 10 ** decimals
    return int(x * f + 1e-9) / f


def get_order_info(api_key, api_secret, sym, order_id, side):
    """side = 'buy' หรือ 'sell', sym เช่น 'btc_thb'"""
    query = f"sym={sym}&id={order_id}&sd={side}"
    return _private_request(api_key, api_secret, "GET", "/api/v3/market/order-info", query=query)


def summarize_fill(info_result, side):
    """
    แปลงผล order-info เป็นตัวเลขที่บอทใช้ คืน None ถ้ายังไม่มีการจับคู่ (filled = 0)
    buy : filled = เงินบาทที่ใช้ไป, ได้เหรียญ = (filled - ค่าธรรมเนียมที่จ่ายเป็นเงินบาท) / rate
    sell: filled = จำนวนเหรียญที่ขายได้, ได้เงิน = filled * rate - ค่าธรรมเนียมที่จ่ายเป็นเงินบาท
    ค่าธรรมเนียมที่ใช้ trading credit จ่ายไป (credit) ไม่ได้หักจากเงิน/เหรียญ
    ถ้า rate หลักเป็น 0 ใช้ราคาเฉลี่ยถ่วงน้ำหนักจาก history แทน
    """
    if not isinstance(info_result, dict):
        return None
    try:
        filled = float(info_result.get("filled") or 0)
        fee = float(info_result.get("fee") or 0)
        credit = float(info_result.get("credit") or 0)
        rate = float(info_result.get("rate") or 0)
    except (TypeError, ValueError):
        return None
    if filled <= 0:
        return None
    if rate <= 0:
        hist = [h for h in info_result.get("history") or [] if float(h.get("rate") or 0) > 0]
        weight = sum(float(h.get("amount") or 0) for h in hist)
        if weight > 0:
            rate = sum(float(h["rate"]) * float(h.get("amount") or 0) for h in hist) / weight
    if rate <= 0:
        return None
    cash_fee = max(fee - credit, 0.0)
    if side == "buy":
        return {"rate": rate, "thb": filled, "coin": (filled - cash_fee) / rate,
                "status": info_result.get("status")}
    return {"rate": rate, "coin": filled, "thb": filled * rate - cash_fee,
            "status": info_result.get("status")}


def fetch_fill(api_key, api_secret, sym, order_id, side, tries=4, delay=1.5):
    """ถาม order-info ซ้ำสั้นๆ จนกว่า market order จะจับคู่เสร็จ คืน (fill หรือ None, raw result)"""
    raw = None
    for i in range(tries):
        try:
            resp = get_order_info(api_key, api_secret, sym, order_id, side)
        except (BitkubAPIError, requests.RequestException) as e:
            raw = {"exception": str(e)}
            resp = None
        if isinstance(resp, dict) and resp.get("error", 0) in (0, None):
            raw = resp.get("result")
            fill = summarize_fill(raw, side)
            if fill and (fill.get("status") == "filled" or i == tries - 1):
                return fill, raw
        elif resp is not None:
            raw = resp
        if i < tries - 1:
            time.sleep(delay)
    return None, raw
