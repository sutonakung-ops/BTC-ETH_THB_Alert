"""เปิด/ปิดโหมดป้องกันจาก GitHub (ใช้หลัง vault unlock) — python tools/set_mode.py on|off"""
import json, os, sys
from datetime import datetime, timezone
import requests

on = sys.argv[1] == "on"
path = "state/control.json"
try:
    ctl = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError):
    ctl = {"paused": False}
ctl.update({"defensive": on, "defensive_updated_at": datetime.now(timezone.utc).isoformat()})
os.makedirs("state", exist_ok=True)
json.dump(ctl, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
msg = ("🛡️ เปิดโหมดป้องกันแล้ว — หยุดซื้อใหม่ทั้งกองปกติและกองช้อน\nยังขายตามกติกาเหมือนเดิม เงินที่ขายได้จะพักเป็นเงินบาท\n"
       "พิมพ์ \"ปิดป้องกัน\" เมื่ออยากให้กลับมาซื้อตามปกติ") if on else "🟢 ปิดโหมดป้องกันแล้ว — กลับมาซื้อตามลำดับปกติ"
if token and chat:
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": "🧱 Steady Stack\n" + msg}, timeout=15)
print("defensive =", on)
