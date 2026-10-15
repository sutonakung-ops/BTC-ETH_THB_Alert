import json
import os
import time
from datetime import datetime, timezone

import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# เหรียญเดียวกับที่ dca_bot.py ซื้อสะสม — ให้ dashboard แสดงราคา/เทรนด์ครบทุกเหรียญที่ถืออยู่จริง
COINS = [
    {"name": "BTC", "candle_symbol": "BTC_THB", "ticker_symbol": "THB_BTC"},
    {"name": "ETH", "candle_symbol": "ETH_THB", "ticker_symbol": "THB_ETH"},
    {"name": "XRP", "candle_symbol": "XRP_THB", "ticker_symbol": "THB_XRP"},
    {"name": "ADA", "candle_symbol": "ADA_THB", "ticker_symbol": "THB_ADA"},
    {"name": "DOGE", "candle_symbol": "DOGE_THB", "ticker_symbol": "THB_DOGE"},
    {"name": "SOL", "candle_symbol": "SOL_THB", "ticker_symbol": "THB_SOL"},
    {"name": "KUB", "candle_symbol": "KUB_THB", "ticker_symbol": "THB_KUB"},
]


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})


def get_candles(symbol, resolution, hours_back):
    now = int(time.time())
    start = now - hours_back * 3600
    url = "https://api.bitkub.com/tradingview/history"
    params = {"symbol": symbol, "resolution": resolution, "from": start, "to": now}
    response = requests.get(url, params=params)
    return response.json()


def moving_average(prices, period):
    return sum(prices[-period:]) / period


def check_change_alert(name, change):
    if change <= -5:
        send_telegram(f"⚠️ เตือน: {name} ร่วงเกิน 5% ใน 24 ชม. ({change}%)")
    elif change >= 5:
        send_telegram(f"🚀 เตือน: {name} พุ่งเกิน 5% ใน 24 ชม. ({change}%)")
    else:
        print(f"{name} ยังอยู่ในช่วงปกติ ({change}%) — ไม่ส่งแจ้งเตือน")


def analyze_trend(name, symbol):
    data = get_candles(symbol, "60", 48)
    closes = data["c"]
    current_price = closes[-1]
    ma12 = moving_average(closes, 12)
    ma24 = moving_average(closes, 24)
    gap_percent = (ma12 - ma24) / ma24 * 100

    print(f"--- {name} เทรนด์ ---")
    print("ราคาปัจจุบัน:", round(current_price, 2))
    print("MA12:", round(ma12, 2), " | MA24:", round(ma24, 2))
    print("ส่วนต่าง:", round(gap_percent, 4), "%")

    if gap_percent > 0.3:
        signal = "Golden Cross (ขาขึ้น)"
        send_telegram(f"📈 {name}: Golden Cross ชัดเจน (แนวโน้มขาขึ้น) ส่วนต่าง {round(gap_percent,2)}%")
    elif gap_percent < -0.3:
        signal = "Death Cross (ขาลง)"
        send_telegram(f"📉 {name}: Death Cross ชัดเจน (แนวโน้มขาลง) ส่วนต่าง {round(gap_percent,2)}%")
    else:
        signal = "ยังสรุปแนวโน้มไม่ได้"
        print(f"{name}: เส้นยังชิดกันเกินไป ยังสรุปแนวโน้มไม่ได้")

    return {
        "price": round(current_price, 2),
        "ma12": round(ma12, 2),
        "ma24": round(ma24, 2),
        "gap_percent": round(gap_percent, 4),
        "signal": signal,
    }


def main():
    # ---- ดึง % เปลี่ยนแปลง 24 ชม. ของทุกเหรียญในครั้งเดียว ----
    response = requests.get("https://api.bitkub.com/api/market/ticker")
    ticker = response.json()

    result = {"updated_at": datetime.now(timezone.utc).isoformat()}

    for coin in COINS:
        name = coin["name"]
        change = ticker.get(coin["ticker_symbol"], {}).get("percentChange")
        if change is None:
            print(f"ข้าม {name}: ไม่พบข้อมูล ticker")
            continue
        check_change_alert(name, change)
        trend = analyze_trend(name, coin["candle_symbol"])
        result[name] = {"change_24h": change, **trend}

    # ---- บันทึกผลล่าสุดไว้ให้หน้าเว็บ Dashboard อ่าน ----
    with open("latest.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("บันทึกไฟล์ latest.json แล้ว")
    print("เช็คราคาเสร็จสิ้น")


if __name__ == "__main__":
    main()
