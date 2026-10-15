# -*- coding: utf-8 -*-
"""
บอท DCA (Dollar-Cost Averaging) สำหรับ Bitkub
- ซื้อเหรียญที่กำหนดไว้ด้วยจำนวนเงินคงที่ "ทุกวัน" (ไม่สน timing/สัญญาณใดๆ)
- ตัดปัญหา overfitting ของการจับจังหวะที่พิสูจน์แล้วว่าไม่มี edge จริงบน Bitkub
  (ดู backtest.py / filter_ablation — ทดสอบ 7 เหรียญ x 8 ชุดตัวกรอง ไม่มีชุดใดกำไรนอกช่วงข้อมูลที่ใช้หา params)
- รันผ่าน GitHub Actions ทุกชั่วโมงเหมือนบอทอื่น แต่จะ "ซื้อจริง" แค่ครั้งแรกของแต่ละวัน
  (เช็คจาก state/dca_state.json ว่าซื้อไปแล้วหรือยังสำหรับวันนี้ตามเวลาไทย)

สถานะทั้งหมดเก็บเป็นไฟล์ json ใต้โฟลเดอร์ state/ แล้วให้ GitHub Actions
commit กลับเข้า repo ทุกครั้งที่รัน (เพราะ Actions runner ใช้แล้วทิ้งไฟล์หายทุกรอบ)
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

from bitkub_trade import BitkubAPIError, place_bid

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BITKUB_API_KEY = os.environ.get("BITKUB_API_KEY", "").strip()
BITKUB_API_SECRET = os.environ.get("BITKUB_API_SECRET", "").strip()

BKK = timezone(timedelta(hours=7))

STATE_DIR = "state"
DCA_STATE_FILE = f"{STATE_DIR}/dca_state.json"
DASHBOARD_FILE = "latest.json"

# ---------------- ตั้งค่า DCA (แก้ตัวเลขตรงนี้ได้เลยถ้าต้องการเปลี่ยน) ----------------
# งบ 50 บาท/เหรียญ/วัน x 7 เหรียญ = 350 บาท/วัน
# ผู้ใช้แจ้งว่าจะ "ทยอยเติมเงิน" เข้าบัญชี Bitkub เรื่อยๆ ตามผลลัพธ์ที่เห็น ไม่ใช่ทุนก้อนเดียว 500-1,000 บาท
# ที่ตั้งไว้ตอนทดสอบระบบจับจังหวะช่วงแรก — ดังนั้นก่อนเปิดใช้งานจริง ควรเติมเงินเข้าบัญชีให้พอใช้ต่อเนื่อง
# และ/หรือลดจำนวนเหรียญ/จำนวนเงินต่อวันลงได้ตามสบาย
DCA_AMOUNT_THB_PER_COIN = 50

DCA_COINS = [
    {"name": "BTC", "trade_symbol": "THB_BTC"},
    {"name": "ETH", "trade_symbol": "THB_ETH"},
    {"name": "XRP", "trade_symbol": "THB_XRP"},
    {"name": "ADA", "trade_symbol": "THB_ADA"},
    {"name": "DOGE", "trade_symbol": "THB_DOGE"},
    {"name": "SOL", "trade_symbol": "THB_SOL"},
    {"name": "KUB", "trade_symbol": "THB_KUB"},
]


# ---------------- ฟังก์ชันพื้นฐาน ----------------

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_ticker():
    """ดึงราคาล่าสุดของทุกคู่เทรดในครั้งเดียว (public API ไม่ต้อง sign)"""
    response = requests.get("https://api.bitkub.com/api/market/ticker", timeout=20)
    return response.json()


def today_str():
    return datetime.now(BKK).strftime("%Y-%m-%d")


def default_state():
    return {
        "last_run_date": None,
        "holdings": {
            coin["name"]: {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0}
            for coin in DCA_COINS
        },
        "log": [],
    }


# ---------------- ฟังก์ชันสั่งซื้อจริง ----------------

def execute_buy(trade_symbol, amount_thb):
    """สั่งซื้อจริงด้วยเงิน amount_thb บาท (market order)"""
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        return {"ok": False, "error": "ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET"}
    try:
        result = place_bid(BITKUB_API_KEY, BITKUB_API_SECRET, trade_symbol, amount_thb, rat=0, typ="market")
    except BitkubAPIError as e:
        return {"ok": False, "error": str(e)}

    res = result.get("result", result)
    if result.get("error", 0) not in (0, None):
        return {"ok": False, "error": f"Bitkub error code {result.get('error')}: {res}"}

    fill_price = None
    for key in ("rat", "last", "avg"):
        val = res.get(key) if isinstance(res, dict) else None
        if val:
            fill_price = float(val)
            break

    amount_coin = None
    for key in ("rec", "amt"):
        val = res.get(key) if isinstance(res, dict) else None
        if val:
            amount_coin = float(val)
            break
    if not amount_coin and fill_price:
        amount_coin = amount_thb / fill_price  # ประมาณการ ยังไม่หักค่าธรรมเนียม

    if not fill_price or not amount_coin:
        return {"ok": False, "error": f"อ่านผลลัพธ์คำสั่งซื้อไม่ได้: {res}"}

    return {"ok": True, "fill_price": fill_price, "amount_coin": amount_coin}


# ---------------- โปรแกรมหลัก ----------------

def main():
    state = load_json(DCA_STATE_FILE, default_state())
    # กันไฟล์เก่าที่ยังไม่มีเหรียญใหม่ (เผื่อขยายรายชื่อเหรียญทีหลัง)
    for coin in DCA_COINS:
        state["holdings"].setdefault(coin["name"], {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0})

    trading_enabled = bool(BITKUB_API_KEY and BITKUB_API_SECRET)
    today = today_str()
    already_bought_today = state.get("last_run_date") == today

    ticker = {}
    try:
        ticker = get_ticker()
    except requests.RequestException as e:
        print(f"ดึงราคาล่าสุดไม่สำเร็จ: {e}")

    if not trading_enabled:
        print("ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET — ข้าม DCA รอบนี้ (ยังไม่ซื้อจริง)")
    elif already_bought_today:
        print(f"ซื้อ DCA ของวันนี้ ({today}) ไปแล้ว รอวันถัดไป")
    else:
        bought_lines = []
        failed_lines = []
        for coin in DCA_COINS:
            name = coin["name"]
            outcome = execute_buy(coin["trade_symbol"], DCA_AMOUNT_THB_PER_COIN)
            if outcome["ok"]:
                h = state["holdings"][name]
                h["total_invested_thb"] = round(h["total_invested_thb"] + DCA_AMOUNT_THB_PER_COIN, 4)
                h["total_coin"] = h["total_coin"] + outcome["amount_coin"]
                h["buy_count"] = h["buy_count"] + 1
                state["log"].append({
                    "date": today,
                    "name": name,
                    "invested_thb": DCA_AMOUNT_THB_PER_COIN,
                    "fill_price": outcome["fill_price"],
                    "amount_coin": outcome["amount_coin"],
                })
                bought_lines.append(f"  {name}: {DCA_AMOUNT_THB_PER_COIN} บาท @ {outcome['fill_price']:.4f}")
            else:
                failed_lines.append(f"  {name}: ล้มเหลว — {outcome['error']}")

        state["log"] = state["log"][-500:]  # กันไฟล์โตไม่จำกัด เก็บแค่ 500 รายการล่าสุด
        state["last_run_date"] = today

        msg = f"💰 DCA วันนี้ ({today}) — ซื้อ {len(bought_lines)}/{len(DCA_COINS)} เหรียญสำเร็จ\n"
        if bought_lines:
            msg += "\n".join(bought_lines)
        if failed_lines:
            msg += "\n⚠️ ล้มเหลว:\n" + "\n".join(failed_lines)
        send_telegram(msg)

    # ----- คำนวณมูลค่าปัจจุบันของแต่ละเหรียญ (ใช้ได้ทั้งตอนเปิด/ปิดใช้งานจริง) -----
    per_coin = {}
    total_invested = 0.0
    total_value = 0.0
    for coin in DCA_COINS:
        name = coin["name"]
        h = state["holdings"][name]
        current_price = None
        tick = ticker.get(coin["trade_symbol"]) if isinstance(ticker, dict) else None
        if isinstance(tick, dict):
            current_price = tick.get("last")
        current_value = (h["total_coin"] * current_price) if current_price else None
        avg_cost = (h["total_invested_thb"] / h["total_coin"]) if h["total_coin"] else None
        pnl_thb = (current_value - h["total_invested_thb"]) if current_value is not None else None
        pnl_pct = (pnl_thb / h["total_invested_thb"] * 100) if pnl_thb is not None and h["total_invested_thb"] else None

        per_coin[name] = {
            "total_invested_thb": round(h["total_invested_thb"], 2),
            "total_coin": h["total_coin"],
            "buy_count": h["buy_count"],
            "avg_cost": round(avg_cost, 4) if avg_cost is not None else None,
            "current_price": current_price,
            "current_value_thb": round(current_value, 2) if current_value is not None else None,
            "pnl_thb": round(pnl_thb, 2) if pnl_thb is not None else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        }
        total_invested += h["total_invested_thb"]
        if current_value is not None:
            total_value += current_value

    total_pnl = total_value - total_invested if total_invested else 0.0
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else None

    save_json(DCA_STATE_FILE, state)

    dashboard = load_json(DASHBOARD_FILE, {})
    dashboard["dca_bot"] = {
        "enabled": trading_enabled,
        "amount_thb_per_coin_per_day": DCA_AMOUNT_THB_PER_COIN,
        "coins": per_coin,
        "total_invested_thb": round(total_invested, 2),
        "total_value_thb": round(total_value, 2) if total_invested else None,
        "total_pnl_thb": round(total_pnl, 2) if total_invested else None,
        "total_pnl_pct": round(total_pnl_pct, 2) if total_pnl_pct is not None else None,
        "last_run_date": state.get("last_run_date"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(DASHBOARD_FILE, dashboard)

    print("dca_bot: ทำงานเสร็จสิ้น")


if __name__ == "__main__":
    main()
