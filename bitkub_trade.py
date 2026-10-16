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


def _private_request(api_key, api_secret, method, path, body_dict=None):
    if not api_key or not api_secret:
        raise BitkubAPIError("ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET")

    timestamp = _timestamp_ms()
    body = json.dumps(body_dict, separators=(",", ":")) if body_dict is not None else ""
    signature = _sign(api_secret, timestamp, method.upper(), path, body)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-BTK-APIKEY": api_key,
        "X-BTK-TIMESTAMP": timestamp,
        "X-BTK-SIGN": signature,
    }

    url = f"{API_BASE}{path}"
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
