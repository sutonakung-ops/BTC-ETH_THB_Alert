# -*- coding: utf-8 -*-
"""
ข้อมูลตลาดเฉพาะของ Bitkub (เก็บไว้วัดผล ยังไม่ใช้ตัดสินใจซื้อขาย จนกว่าจะพิสูจน์ได้ว่าช่วยจริง)

1. premium_pct  — ราคาบน Bitkub แพง/ถูกกว่าตลาดโลกกี่ %
                  ราคาโลก = ราคา Binance (USDT) x ราคา USDT บน Bitkub (บาท)
                  บวกมาก = Bitkub แพงกว่าโลก มีแนวโน้มไหลลงหาราคาโลก
2. depth_imbalance — คำสั่งซื้อเทียบคำสั่งขายที่รออยู่ในกระดาน ภายใน ±1% จากราคากลาง (มูลค่าเป็นบาท)
                  -1 ถึง +1: บวก = ฝั่งซื้อหนากว่า (มีแรงรับ), ลบ = ฝั่งขายหนากว่า (มีแรงกด)
3. buy_flow_pct — จากรายการซื้อขายล่าสุด สัดส่วนมูลค่าที่เป็นฝั่ง "ไล่ซื้อ" (BUY) กี่ %
                  เกิน 50% = คนไล่ซื้อมากกว่าเทขาย

ทุกฟังก์ชันคืน None ถ้าดึงข้อมูลไม่ได้ (ไม่ทำให้บอทหยุดทำงาน)
"""

import requests

BITKUB = "https://api.bitkub.com"
# endpoint ข้อมูลตลาดสาธารณะของ Binance (อ่านอย่างเดียว ใช้ได้จากเซิร์ฟเวอร์ทั่วโลกรวมถึง GitHub Actions)
BINANCE_DATA = "https://data-api.binance.vision"

DEPTH_BAND_PCT = 1.0
DEPTH_LIMIT = 200
TRADES_LIMIT = 200


def fetch_global_usdt_prices():
    """ราคาล่าสุดทุกคู่ XXXUSDT บน Binance -> {"BTC": 67000.0, ...}"""
    try:
        resp = requests.get(f"{BINANCE_DATA}/api/v3/ticker/price", timeout=15)
        rows = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"ดึงราคาตลาดโลก (Binance) ไม่สำเร็จ: {e}")
        return {}
    prices = {}
    if isinstance(rows, list):
        for r in rows:
            sym = r.get("symbol", "")
            if sym.endswith("USDT"):
                try:
                    prices[sym[:-4]] = float(r["price"])
                except (KeyError, TypeError, ValueError):
                    pass
    return prices


def premium_pct(name, bitkub_ticker, global_prices):
    try:
        thb_price = float(bitkub_ticker[f"THB_{name}"]["last"])
        thb_per_usdt = float(bitkub_ticker["THB_USDT"]["last"])
        usdt_price = global_prices[name]
    except (KeyError, TypeError, ValueError):
        return None
    if not usdt_price or not thb_per_usdt:
        return None
    return round((thb_price / (usdt_price * thb_per_usdt) - 1) * 100, 3)


def depth_imbalance(name):
    try:
        resp = requests.get(f"{BITKUB}/api/v3/market/depth",
                            params={"sym": f"{name.lower()}_thb", "lmt": DEPTH_LIMIT}, timeout=15)
        book = resp.json().get("result") or {}
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            return None
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        lo, hi = mid * (1 - DEPTH_BAND_PCT / 100), mid * (1 + DEPTH_BAND_PCT / 100)
        bid_thb = sum(float(p) * float(q) for p, q in bids if float(p) >= lo)
        ask_thb = sum(float(p) * float(q) for p, q in asks if float(p) <= hi)
    except (requests.RequestException, ValueError, TypeError, IndexError, AttributeError) as e:
        print(f"{name}: ดึงกระดานคำสั่งซื้อขายไม่สำเร็จ: {e}")
        return None
    if bid_thb + ask_thb <= 0:
        return None
    return round((bid_thb - ask_thb) / (bid_thb + ask_thb), 3)


def buy_flow_pct(name):
    try:
        resp = requests.get(f"{BITKUB}/api/v3/market/trades",
                            params={"sym": f"{name.lower()}_thb", "lmt": TRADES_LIMIT}, timeout=15)
        trades = resp.json().get("result") or []
        buy = sum(float(t[1]) * float(t[2]) for t in trades if str(t[3]).upper() == "BUY")
        total = sum(float(t[1]) * float(t[2]) for t in trades)
    except (requests.RequestException, ValueError, TypeError, IndexError, AttributeError) as e:
        print(f"{name}: ดึงรายการซื้อขายล่าสุดไม่สำเร็จ: {e}")
        return None
    if total <= 0:
        return None
    return round(buy / total * 100, 1)


def market_features(name, bitkub_ticker, global_prices):
    return {
        "premium_pct": premium_pct(name, bitkub_ticker, global_prices),
        "depth_imbalance": depth_imbalance(name),
        "buy_flow_pct": buy_flow_pct(name),
    }
