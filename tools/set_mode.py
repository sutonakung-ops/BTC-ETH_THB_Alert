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
sys.path.insert(0, os.getcwd())
import efds  # noqa: E402
ctl = efds.set_manual(ctl, on)
os.makedirs("state", exist_ok=True)
json.dump(ctl, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
msg = ("🛡️ เปิด EFDS แล้ว (สั่งเอง) — หยุดซื้อใหม่ทั้งกองปกติและกองช้อน\nยังขายตามกติกาเหมือนเดิม\n"
       "พิมพ์ \"ปิด EFDS\" เมื่ออยากให้กลับมาซื้อตามปกติ") if on else "🟢 ปิด EFDS แล้ว — กลับมาซื้อตามลำดับปกติ"
if token and chat:
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": "🧱 Steady Stack\n" + msg}, timeout=15)
print("defensive =", on)
