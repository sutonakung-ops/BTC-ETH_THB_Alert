import os
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})

def check_alert(name, change):
    if change <= -5:
        send_telegram(f"⚠️ เตือน: {name} ร่วงเกิน 5% ใน 24 ชม. ({change}%)")
    elif change >= 5:
        send_telegram(f"🚀 เตือน: {name} พุ่งเกิน 5% ใน 24 ชม. ({change}%)")
    else:
        print(f"{name} ยังอยู่ในช่วงปกติ ({change}%) — ไม่ส่งแจ้งเตือน")

response = requests.get("https://api.bitkub.com/api/market/ticker")
data = response.json()

btc_change_24h = data["THB_BTC"]["percentChange"]
eth_change_24h = data["THB_ETH"]["percentChange"]

check_alert("BTC", btc_change_24h)
check_alert("ETH", eth_change_24h)

print("เช็คราคาเสร็จสิ้น")
