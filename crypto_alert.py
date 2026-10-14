import os
import time
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

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
        send_telegram(f"📈 {name}: Golden Cross ชัดเจน (แนวโน้มขาขึ้น) ส่วนต่าง {round(gap_percent,2)}%")
    elif gap_percent < -0.3:
        send_telegram(f"📉 {name}: Death Cross ชัดเจน (แนวโน้มขาลง) ส่วนต่าง {round(gap_percent,2)}%")
    else:
        print(f"{name}: เส้นยังชิดกันเกินไป ยังสรุปแนวโน้มไม่ได้")

# ---- ตรวจ % เปลี่ยนแปลง 24 ชม. (ของเดิม) ----
response = requests.get("https://api.bitkub.com/api/market/ticker")
data = response.json()

check_change_alert("BTC", data["THB_BTC"]["percentChange"])
check_change_alert("ETH", data["THB_ETH"]["percentChange"])

# ---- วิเคราะห์เทรนด์ด้วย MA (ของใหม่) ----
analyze_trend("BTC", "BTC_THB")
analyze_trend("ETH", "ETH_THB")

print("เช็คราคาเสร็จสิ้น")
