# -*- coding: utf-8 -*-
"""
บอทเทรดกึ่งอัตโนมัติ (semi-automatic) สำหรับ Bitkub
- หาสัญญาณซื้อด้วย MA12/MA24 Golden Cross + กรองด้วย RSI และปริมาณซื้อขาย
- เปิดไม้ใหม่ "ต้องรอยืนยันทาง Telegram ก่อน" (พิมพ์ "ยืนยัน" หรือ "ยกเลิก" ตอบกลับบอท)
- ปิดไม้ (stop-loss / take-profit / สัญญาณกลับตัว) ทำอัตโนมัติทันที ไม่ต้องรอยืนยัน
- มีเพดานความเสี่ยงรายวัน ถ้าขาดทุนสะสมเกินที่กำหนด จะหยุดเปิดไม้ใหม่จนถึงวันถัดไป

สถานะทั้งหมดเก็บเป็นไฟล์ json ใต้โฟลเดอร์ state/ แล้วให้ GitHub Actions
commit กลับเข้า repo ทุกครั้งที่รัน (เพราะ Actions runner ใช้แล้วทิ้งไฟล์หายทุกรอบ)
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from bitkub_trade import BitkubAPIError, place_ask, place_bid
from strategy import check_exit, compute_indicators, is_entry_signal

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BITKUB_API_KEY = os.environ.get("BITKUB_API_KEY", "").strip()
BITKUB_API_SECRET = os.environ.get("BITKUB_API_SECRET", "").strip()

BKK = timezone(timedelta(hours=7))

STATE_DIR = "state"
POSITION_FILE = f"{STATE_DIR}/position.json"
PENDING_FILE = f"{STATE_DIR}/pending_signal.json"
TRADE_LOG_FILE = f"{STATE_DIR}/trade_log.json"
RISK_FILE = f"{STATE_DIR}/daily_risk.json"
TG_OFFSET_FILE = f"{STATE_DIR}/telegram_offset.json"
DASHBOARD_FILE = "latest.json"

# ---------------- ตั้งค่าความเสี่ยง (แก้ตัวเลขตรงนี้ได้เลยถ้าต้องการเปลี่ยน) ----------------
TRADE_CAPITAL_THB = 1000       # เงินทุนที่ใช้ต่อการเปิด 1 ไม้ (รอบทดสอบ: 500-1000 บาท)
STOP_LOSS_PCT = 4.0            # ตัดขาดทุนอัตโนมัติเมื่อราคาร่วงจากจุดเข้า X%
TAKE_PROFIT_PCT = 6.0          # ปิดทำกำไรอัตโนมัติเมื่อราคาขึ้นจากจุดเข้า X%
DAILY_MAX_LOSS_PCT = 10.0      # ขาดทุนสะสมวันนี้เกิน X% ของทุน -> หยุดเปิดไม้ใหม่ทั้งวัน
SIGNAL_EXPIRE_MINUTES = 90     # สัญญาณที่ส่งไปรอยืนยัน ถ้าเกินกี่นาทีไม่ตอบ ให้ยกเลิกอัตโนมัติ
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70            # ถ้า RSI สูงกว่านี้ ถือว่าซื้อแพงเกินไป ไม่เข้าไม้ใหม่
MA_GAP_THRESHOLD = 0.3         # ส่วนต่าง MA12/MA24 ต้องมากกว่านี้ถึงจะถือเป็นสัญญาณจริง

COINS = [
    {"name": "BTC", "candle_symbol": "BTC_THB", "trade_symbol": "THB_BTC"},
    {"name": "ETH", "candle_symbol": "ETH_THB", "trade_symbol": "THB_ETH"},
]

CONFIRM_WORDS = {"yes", "y", "ใช่", "ยืนยัน", "/confirm", "confirm"}
CANCEL_WORDS = {"no", "n", "ไม่", "ยกเลิก", "/cancel", "cancel"}


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


def get_candles(symbol, resolution, hours_back):
    now = int(time.time())
    start = now - hours_back * 3600
    url = "https://api.bitkub.com/tradingview/history"
    params = {"symbol": symbol, "resolution": resolution, "from": start, "to": now}
    response = requests.get(url, params=params, timeout=20)
    return response.json()


def get_telegram_replies(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 0}
    try:
        resp = requests.get(url, params=params, timeout=15)
        return resp.json().get("result", [])
    except requests.RequestException:
        return []


def today_str():
    return datetime.now(BKK).strftime("%Y-%m-%d")


# ---------------- ฟังก์ชันสั่งซื้อ/ขายจริง ----------------

def execute_entry(coin_name, trade_symbol, signal_price):
    """สั่งซื้อจริงด้วยเงิน TRADE_CAPITAL_THB บาท (market order)"""
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        return {"ok": False, "error": "ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET"}
    try:
        result = place_bid(
            BITKUB_API_KEY, BITKUB_API_SECRET, trade_symbol, TRADE_CAPITAL_THB, rat=0, typ="market"
        )
    except BitkubAPIError as e:
        return {"ok": False, "error": str(e)}

    res = result.get("result", result)
    if result.get("error", 0) not in (0, None):
        return {"ok": False, "error": f"Bitkub error code {result.get('error')}: {res}"}

    # พยายามอ่านราคาที่ได้จริงจากผลลัพธ์ API, ถ้าไม่เจอ field ที่คาดไว้ ใช้ราคาตอนส่งสัญญาณแทน (ประมาณการ)
    fill_price = None
    for key in ("rat", "last", "avg"):
        val = res.get(key) if isinstance(res, dict) else None
        if val:
            fill_price = float(val)
            break
    if not fill_price:
        fill_price = signal_price

    amount_coin = None
    for key in ("rec", "amt"):
        val = res.get(key) if isinstance(res, dict) else None
        if val:
            amount_coin = float(val)
            break
    if not amount_coin:
        amount_coin = TRADE_CAPITAL_THB / fill_price  # ประมาณการ ยังไม่หักค่าธรรมเนียม

    position = {
        "name": coin_name,
        "trade_symbol": trade_symbol,
        "entry_price": fill_price,
        "amount_coin": amount_coin,
        "capital_thb": TRADE_CAPITAL_THB,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "raw_api_response": res,
    }
    return {"ok": True, "position": position}


def execute_exit(position, current_price):
    """สั่งขายจริงทั้งหมดที่ถืออยู่ (market order)"""
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        return {"ok": False, "error": "ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET"}
    try:
        result = place_ask(
            BITKUB_API_KEY, BITKUB_API_SECRET, position["trade_symbol"],
            position["amount_coin"], rat=0, typ="market",
        )
    except BitkubAPIError as e:
        return {"ok": False, "error": str(e)}

    res = result.get("result", result)
    if result.get("error", 0) not in (0, None):
        return {"ok": False, "error": f"Bitkub error code {result.get('error')}: {res}"}

    sell_price = None
    for key in ("rat", "last", "avg"):
        val = res.get(key) if isinstance(res, dict) else None
        if val:
            sell_price = float(val)
            break
    if not sell_price:
        sell_price = current_price

    proceeds_thb = None
    for key in ("rec",):
        val = res.get(key) if isinstance(res, dict) else None
        if val:
            proceeds_thb = float(val)
            break
    if not proceeds_thb:
        proceeds_thb = position["amount_coin"] * sell_price

    pnl_thb = proceeds_thb - position["capital_thb"]
    return {"ok": True, "sell_price": sell_price, "proceeds_thb": proceeds_thb, "pnl_thb": pnl_thb, "raw_api_response": res}


# ---------------- โปรแกรมหลัก ----------------

def main():
    position = load_json(POSITION_FILE, None)
    pending = load_json(PENDING_FILE, None)
    had_pending_at_start = pending is not None
    trade_log = load_json(TRADE_LOG_FILE, [])
    risk = load_json(RISK_FILE, {})
    tg_state = load_json(TG_OFFSET_FILE, {"offset": 0})

    if risk.get("date") != today_str():
        risk = {"date": today_str(), "realized_pnl_thb": 0.0, "halted": False}

    trading_enabled = bool(BITKUB_API_KEY and BITKUB_API_SECRET)
    if not trading_enabled:
        print("ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET — ข้ามการเทรดจริง (วิเคราะห์อย่างเดียว)")

    # ----- 1. ดึงราคา + คำนวณ indicator ของทุกเหรียญ (ใช้สูตรเดียวกับ backtest.py จาก strategy.py) -----
    market = {}
    for coin in COINS:
        data = get_candles(coin["candle_symbol"], "60", 48)
        closes = data["c"]
        volumes = data.get("v", [])
        indicators = compute_indicators(closes, volumes, rsi_period=RSI_PERIOD)
        indicators["trade_symbol"] = coin["trade_symbol"]
        market[coin["name"]] = indicators

    # ----- 2. อ่านข้อความตอบกลับจาก Telegram -----
    confirm_seen, cancel_seen = False, False
    replies = get_telegram_replies(tg_state.get("offset", 0))
    new_offset = tg_state.get("offset", 0)
    for upd in replies:
        new_offset = max(new_offset, upd["update_id"] + 1)
        msg = upd.get("message", {})
        if str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue
        text = (msg.get("text") or "").strip().lower()
        if text in CONFIRM_WORDS:
            confirm_seen = True
        elif text in CANCEL_WORDS:
            cancel_seen = True
    tg_state["offset"] = new_offset

    # ----- 3. ถ้ามีสัญญาณค้างรอยืนยันอยู่ -----
    if pending:
        created = datetime.fromisoformat(pending["created_at"])
        age_minutes = (datetime.now(timezone.utc) - created).total_seconds() / 60

        if cancel_seen:
            send_telegram(f"❌ ยกเลิกคำสั่งซื้อ {pending['name']} ตามที่แจ้งแล้ว")
            pending = None
        elif confirm_seen:
            if not trading_enabled:
                send_telegram("⚠️ ยืนยันรับทราบ แต่ยังไม่ได้ตั้งค่า Bitkub API key จึงสั่งซื้อจริงไม่ได้")
                pending = None
            else:
                outcome = execute_entry(pending["name"], pending["trade_symbol"], pending["signal_price"])
                if outcome["ok"]:
                    position = outcome["position"]
                    send_telegram(
                        f"✅ เปิดไม้ {pending['name']} สำเร็จ\n"
                        f"ราคาเข้า ~{position['entry_price']:.2f} บาท | ทุน {position['capital_thb']} บาท\n"
                        f"จะตัดขาดทุนอัตโนมัติที่ -{STOP_LOSS_PCT}% หรือปิดกำไรที่ +{TAKE_PROFIT_PCT}%"
                    )
                else:
                    send_telegram(f"⚠️ สั่งซื้อ {pending['name']} ไม่สำเร็จ: {outcome['error']}")
                pending = None
        elif age_minutes > SIGNAL_EXPIRE_MINUTES:
            send_telegram(f"⌛ สัญญาณซื้อ {pending['name']} หมดอายุแล้ว (รอเกิน {SIGNAL_EXPIRE_MINUTES} นาที) ข้ามรอบนี้ไป")
            pending = None
        else:
            print(f"รอการยืนยัน {pending['name']} อยู่ (ผ่านมา {age_minutes:.0f} นาที)")

    # ----- 4. ถ้ามีไม้เปิดอยู่ ตรวจเงื่อนไขปิดไม้ (ทำอัตโนมัติ ไม่ต้องรอยืนยัน) -----
    if position:
        name = position["name"]
        cur_price = market[name]["price"]
        reason_code, change_pct = check_exit(
            market[name], position["entry_price"], STOP_LOSS_PCT, TAKE_PROFIT_PCT, MA_GAP_THRESHOLD
        )
        exit_reason = None
        if reason_code and reason_code.startswith("stop-loss"):
            exit_reason = f"ตัดขาดทุนอัตโนมัติ ({reason_code})"
        elif reason_code and reason_code.startswith("take-profit"):
            exit_reason = f"ปิดทำกำไรอัตโนมัติ ({reason_code})"
        elif reason_code == "death-cross":
            exit_reason = "สัญญาณกลับตัว (Death Cross)"

        if exit_reason:
            if not trading_enabled:
                print(f"{name}: ถึงเงื่อนไขปิดไม้ ({exit_reason}) แต่ยังไม่ได้ตั้งค่า API key")
            else:
                outcome = execute_exit(position, cur_price)
                if outcome["ok"]:
                    risk["realized_pnl_thb"] = risk.get("realized_pnl_thb", 0.0) + outcome["pnl_thb"]
                    trade_log.append({
                        "name": name,
                        "entry_price": position["entry_price"],
                        "exit_price": outcome["sell_price"],
                        "capital_thb": position["capital_thb"],
                        "pnl_thb": round(outcome["pnl_thb"], 2),
                        "reason": exit_reason,
                        "opened_at": position["opened_at"],
                        "closed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    send_telegram(
                        f"🔔 ปิดไม้ {name} อัตโนมัติ: {exit_reason}\n"
                        f"เข้า {position['entry_price']:.2f} → ออก {outcome['sell_price']:.2f} บาท ({change_pct:+.2f}%)\n"
                        f"กำไร/ขาดทุนไม้นี้: {outcome['pnl_thb']:+.2f} บาท | สะสมวันนี้: {risk['realized_pnl_thb']:+.2f} บาท"
                    )
                    position = None
                else:
                    send_telegram(f"⚠️ ปิดไม้ {name} ไม่สำเร็จ: {outcome['error']} (ลองใหม่รอบหน้า)")
        else:
            print(f"{name}: ถือไม้อยู่ เปลี่ยนแปลง {change_pct:+.2f}% ยังไม่ถึงเงื่อนไขปิด")

    # ----- 5. เช็คเพดานความเสี่ยงรายวัน -----
    loss_cap_thb = TRADE_CAPITAL_THB * DAILY_MAX_LOSS_PCT / 100
    if risk.get("realized_pnl_thb", 0.0) <= -loss_cap_thb:
        if not risk.get("halted"):
            send_telegram(
                f"🛑 วันนี้ขาดทุนสะสมเกิน {DAILY_MAX_LOSS_PCT}% ของทุน "
                f"({risk['realized_pnl_thb']:+.2f} บาท) — บอทจะหยุดเปิดไม้ใหม่จนถึงวันถัดไป"
            )
        risk["halted"] = True

    # ----- 6. ถ้าว่างไม้ ไม่มีสัญญาณค้าง (ตั้งแต่ต้นรอบนี้) และไม่โดนพัก ให้หาโอกาสเข้าไม้ใหม่ -----
    # ใช้ had_pending_at_start แทน pending เฉยๆ เพื่อไม่ให้ยกเลิก/หมดอายุสัญญาณแล้วเกิดสัญญาณใหม่ซ้ำในรอบเดียวกัน
    # (รอรอบถัดไปก่อน ให้เวลาผู้ใช้คิดจริงๆ)
    if not position and not pending and not had_pending_at_start and not risk.get("halted"):
        for coin in COINS:
            name = coin["name"]
            info = market[name]
            if is_entry_signal(info, MA_GAP_THRESHOLD, RSI_OVERBOUGHT):
                pending = {
                    "name": name,
                    "trade_symbol": info["trade_symbol"],
                    "signal_price": info["price"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                send_telegram(
                    f"📈 พบสัญญาณเข้าซื้อ {name}\n"
                    f"ราคา {info['price']:.2f} บาท | RSI {info['rsi']} | ส่วนต่าง MA {info['gap_percent']:+.2f}% | ปริมาณซื้อขายยืนยัน ✅\n"
                    f"จะใช้ทุน {TRADE_CAPITAL_THB} บาท | stop-loss {STOP_LOSS_PCT}% | take-profit {TAKE_PROFIT_PCT}%\n\n"
                    f"ตอบกลับ \"ยืนยัน\" ภายใน {SIGNAL_EXPIRE_MINUTES} นาทีเพื่อสั่งซื้อจริง หรือ \"ยกเลิก\" เพื่อข้ามสัญญาณนี้"
                )
                break  # เปิดได้ทีละ 1 ไม้ต่อรอบ เพื่อคุมความเสี่ยง

    # ----- บันทึกสถานะทั้งหมด -----
    save_json(POSITION_FILE, position)
    save_json(PENDING_FILE, pending)
    save_json(TRADE_LOG_FILE, trade_log)
    save_json(RISK_FILE, risk)
    save_json(TG_OFFSET_FILE, tg_state)

    # ----- อัปเดตหน้า dashboard ให้โชว์สถานะบอทเทรดด้วย -----
    dashboard = load_json(DASHBOARD_FILE, {})
    dashboard["trading_bot"] = {
        "enabled": trading_enabled,
        "position": position,
        "pending_signal": pending,
        "daily_risk": risk,
        "trade_count_today": len([t for t in trade_log if t["closed_at"].startswith(today_str())]),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(DASHBOARD_FILE, dashboard)

    print("trading_bot: ทำงานเสร็จสิ้น")


if __name__ == "__main__":
    main()
