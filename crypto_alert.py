import json
import re
import os
import statistics
import time
from datetime import datetime, timezone

import requests

import bitkub_market

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_DIR = "state"
TG_OFFSET_FILE = f"{STATE_DIR}/telegram_offset.json"
CONTROL_FILE = f"{STATE_DIR}/control.json"
COMMANDS_FILE = f"{STATE_DIR}/telegram_commands.json"  # ไฟล์ชั่วคราว ให้ trading_bot.py อ่านต่อในรอบเดียวกัน

# จำนวนเหรียญ "ติดเทรนด์" สูงสุดที่จะดึงเข้า dashboard/DCA อัตโนมัติ
# เกณฑ์: เรียงตามปริมาณซื้อขาย 24 ชม. (quoteVolume หน่วยบาท) บน Bitkub มากไปน้อย
TOP_N_TRENDING = 10
ALWAYS_INCLUDE = ["BTC", "ETH"]
MAX_DEPOSIT_THB = 10000  # กันพิมพ์ผิดหลัก
DEPOSIT_INBOX_FILE = "state/deposit_inbox.json"  # dca_bot.py อ่านแล้วเติมเข้ากองเงินบอท
DCA_STATE_FILE = "state/dca_state.json"  # อ่านรายการเหรียญที่ถืออยู่ เพื่อวิเคราะห์สัญญาณให้ครบ

# Stablecoin ผูกค่ากับ USD 1:1 ราคาแทบไม่ขยับ แม้ปริมาณซื้อขายจะสูงก็ไม่มีความหมายสำหรับ DCA
# (เป้าหมายคือสะสมเหรียญที่มีโอกาสโต ไม่ใช่ถือเงินตรารูปแบบหนึ่งไปอีกรูปแบบหนึ่ง) ตัดออกจากการจัดอันดับเทรนด์เสมอ
STABLECOIN_EXCLUDE = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDD", "FDUSD", "PYUSD", "THBEX"}

# คำสั่งควบคุมผ่าน Telegram (ตอบกลับแชทบอท)
STOP_WORDS = {"หยุด", "หยุดทุกอย่าง", "หยุดบอท", "stop", "/stop", "halt", "/halt"}
START_WORDS = {"เริ่ม", "เริ่มใหม่", "ทำงานต่อ", "start", "/start", "resume", "/resume"}
CONFIRM_WORDS = {"yes", "y", "ใช่", "ยืนยัน", "/confirm", "confirm"}
CANCEL_WORDS = {"no", "n", "ไม่", "ยกเลิก", "/cancel", "cancel"}


BOT_NAME = "Steady Stack"


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    text = f"🧱 {BOT_NAME}\n{message}"  # ทุกข้อความขึ้นต้นด้วยชื่อบอท
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)


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


def moving_average(prices, period):
    return sum(prices[-period:]) / period


ALERT_STEP_PCT = 5  # แจ้งเตือนเฉพาะเหรียญที่ขยับเกิน 5% ใน 24 ชม.
ALERT_STATE_FILE = "state/alert_state.json"


def check_change_alert(name, change, alert_state):
    """
    แจ้งเตือนเมื่อเหรียญขยับเกิน 5% ใน 24 ชม. — แจ้งครั้งเดียวตอนเพิ่งข้ามเกณฑ์ ไม่แจ้งซ้ำทุกชั่วโมง
    จะแจ้งอีกครั้งเมื่อขยับแรงขึ้นอีกขั้นละ 5% (เช่น 10%, 15%) หรือกลับทิศ
    และเริ่มนับใหม่เมื่อกลับมาอยู่ในช่วง ±5%
    """
    level = int(change / ALERT_STEP_PCT)  # 7% -> 1, 12% -> 2, -6% -> -1, 3% -> 0
    last = alert_state.get(name, 0)
    if level == 0:
        alert_state[name] = 0
        print(f"{name} ยังอยู่ในช่วงปกติ ({change}%) — ไม่ส่งแจ้งเตือน")
        return
    if (level > 0) != (last > 0) or abs(level) > abs(last):
        threshold = abs(level) * ALERT_STEP_PCT
        if level < 0:
            send_telegram(f"⚠️ เตือน: {name} ร่วงเกิน {threshold}% ใน 24 ชม. ({change}%)")
        else:
            send_telegram(f"🚀 เตือน: {name} พุ่งเกิน {threshold}% ใน 24 ชม. ({change}%)")
        alert_state[name] = level
    else:
        print(f"{name} ขยับ {change}% — แจ้งเตือนระดับนี้ไปแล้ว ไม่ส่งซ้ำ")


def compute_risk_pct(closes):
    """
    'ความเสี่ยง' ของเหรียญ = ความผันผวนราคาย้อนหลัง 48 ชม. วัดจาก
    ส่วนเบี่ยงเบนมาตรฐานของราคาปิดรายชั่วโมง หารด้วยราคาเฉลี่ยในช่วงเดียวกัน (เป็น %)
    ยิ่งราคาแกว่งแรงในช่วงที่ผ่านมา ยิ่งถือว่าเสี่ยงสูง (ใช้เลือกเหรียญ DCA ร่วมกับข่าว)
    """
    mean_price = statistics.mean(closes)
    if not mean_price:
        return None
    return statistics.pstdev(closes) / mean_price * 100


def fetch_market_sentiment():
    """
    ดึง Fear & Greed Index ของตลาดคริปโตรวม จาก alternative.me (ฟรี ไม่ต้องสมัคร ไม่ต้องมีคีย์)
    แปลงสเกล 0 (Extreme Fear) - 100 (Extreme Greed) เป็น -1 ถึง +1 (ใช้ร่วมกับ risk_pct เลือกเหรียญ DCA)
    หมายเหตุ: เป็นเซนติเมนต์ "รวมทั้งตลาด" ไม่ใช่รายเหรียญ (เดิมตั้งใจใช้ CryptoPanic ที่ให้ข่าวรายเหรียญ
    แต่ปี 2026 CryptoPanic เปลี่ยนเป็นต้องสมัครแพลนเสียเงินเท่านั้น จึงเปลี่ยนมาใช้ตัวนี้แทน)
    ถ้าดึงไม่สำเร็จ ถือเป็นกลาง (0.0) เสมอ ไม่ทำให้รอบทำงานล้มเหลว
    """
    try:
        resp = requests.get("https://api.alternative.me/fng/", params={"limit": 1}, timeout=15)
        data = resp.json().get("data", [])
        value = float(data[0]["value"])
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"ดึง Fear & Greed Index ไม่สำเร็จ ({e}) — ถือว่าตลาดเป็นกลาง")
        return 0.0
    sentiment = (value - 50.0) / 50.0
    return max(-1.0, min(1.0, sentiment))


def analyze_trend(name, symbol, market_sentiment):
    data = get_candles(symbol, "60", 48)
    closes = data.get("c")
    if not closes or len(closes) < 24:
        raise ValueError(f"ข้อมูลแท่งเทียนของ {name} ไม่พอคำนวณ MA (อาจเป็นเหรียญใหม่/เทรดบางเบามาก)")
    current_price = closes[-1]
    ma12 = moving_average(closes, 12)
    ma24 = moving_average(closes, 24)
    gap_percent = (ma12 - ma24) / ma24 * 100
    risk_pct = compute_risk_pct(closes[-48:])
    news_sentiment = market_sentiment

    print(f"--- {name} เทรนด์ ---")
    print("ราคาปัจจุบัน:", round(current_price, 2))
    print("MA12:", round(ma12, 2), " | MA24:", round(ma24, 2))
    print("ส่วนต่าง:", round(gap_percent, 4), "%")
    print("ความเสี่ยง (ผันผวน 48 ชม.):", round(risk_pct, 4) if risk_pct is not None else "-", "%")
    print("ข่าว/เซนติเมนต์:", round(news_sentiment, 3))

    if gap_percent > 0.3:
        signal = "Golden Cross (ขาขึ้น)"  # แสดงบน Dashboard เท่านั้น ไม่ส่ง Telegram (ผู้ใช้ขอให้แจ้งเฉพาะขยับเกิน 5%)
    elif gap_percent < -0.3:
        signal = "Death Cross (ขาลง)"
    else:
        signal = "ยังสรุปแนวโน้มไม่ได้"
        print(f"{name}: เส้นยังชิดกันเกินไป ยังสรุปแนวโน้มไม่ได้")

    return {
        "price": round(current_price, 2),
        "ma12": round(ma12, 2),
        "ma24": round(ma24, 2),
        "gap_percent": round(gap_percent, 4),
        "signal": signal,
        "risk_pct": round(risk_pct, 4) if risk_pct is not None else None,
        "news_sentiment": round(news_sentiment, 3),
        "closes": [round(c, 6) for c in closes[-48:]],
    }


def pick_trending_coins(ticker, top_n):
    """
    เรียงทุกคู่เทรด THB_* บน Bitkub ตามปริมาณซื้อขาย 24 ชม. (quoteVolume, หน่วยบาท) มากไปน้อย
    คืนรายชื่อ top_n อันดับแรก (ไม่รวมคู่ที่ถูกแช่แข็ง/isFrozen)
    """
    rows = []
    if not isinstance(ticker, dict):
        return rows
    for ticker_symbol, info in ticker.items():
        if not isinstance(ticker_symbol, str) or not ticker_symbol.startswith("THB_"):
            continue
        if not isinstance(info, dict):
            continue
        if str(info.get("isFrozen", "0")) == "1":
            continue
        name = ticker_symbol[len("THB_"):]
        if name in STABLECOIN_EXCLUDE:
            continue
        try:
            volume = float(info.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        rows.append({"name": name, "ticker_symbol": ticker_symbol, "candle_symbol": f"{name}_THB", "quote_volume_thb": volume})
    rows.sort(key=lambda r: r["quote_volume_thb"], reverse=True)
    top = rows[:top_n]
    # BTC และ ETH อยู่ในตัวเลือกเสมอ แม้ไม่ติดอันดับปริมาณซื้อขาย (ผู้ใช้ขอ "ไม่เว้น BTC และ ETH")
    for must in ALWAYS_INCLUDE:
        if not any(r["name"] == must for r in top):
            top += [r for r in rows if r["name"] == must]
    return top


def process_telegram_commands():
    """
    อ่านข้อความตอบกลับ Telegram ที่ยังไม่ประมวลผล (offset เดียว ใช้ร่วมกันทุกสคริปต์)
    - หยุด/เริ่ม -> แก้ state/control.json (paused) ทันที มีผลกับ trading_bot.py และ dca_bot.py รอบถัดไปในงานเดียวกัน
    - ยืนยัน/ยกเลิก -> ไม่ประมวลผลที่นี่ แค่ส่งต่อให้ trading_bot.py ผ่าน state/telegram_commands.json
      (ทำแบบนี้เพราะ Telegram getUpdates ใช้ offset ตัวเดียวร่วมกันทั้งบอท ถ้าแต่ละสคริปต์ดึงเองจะแย่งกันเห็นข้อความ)
    """
    control = load_json(CONTROL_FILE, {"paused": False, "updated_at": None, "reason": None})
    tg_state = load_json(TG_OFFSET_FILE, {"offset": 0})

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": tg_state.get("offset", 0), "timeout": 0}, timeout=15)
        replies = resp.json().get("result", [])
    except requests.RequestException as e:
        print(f"ดึงข้อความ Telegram ไม่สำเร็จ: {e}")
        replies = []

    confirm_seen, cancel_seen = False, False
    new_offset = tg_state.get("offset", 0)
    for upd in replies:
        new_offset = max(new_offset, upd["update_id"] + 1)
        msg = upd.get("message", {})
        if str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue
        text = (msg.get("text") or "").strip().lower()
        if text in STOP_WORDS:
            if not control.get("paused"):
                control = {
                    "paused": True,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "reason": f"telegram: {text}",
                }
                send_telegram(
                    "🛑 หยุดการทำงานทั้งหมดแล้ว (DCA bot และ trading bot)\n"
                    "จะไม่มีการซื้อ/ขายใดๆ เกิดขึ้นอีก รวมถึง stop-loss/take-profit อัตโนมัติก็จะไม่ทำงานด้วย "
                    "จนกว่าจะพิมพ์ \"เริ่ม\" เพื่อให้ทำงานต่อ\n"
                    "(ราคาและ dashboard ยังอัปเดตตามปกติ แค่ไม่มีคำสั่งซื้อขายใหม่)"
                )
        elif text in START_WORDS:
            if control.get("paused"):
                control = {
                    "paused": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "reason": f"telegram: {text}",
                }
                send_telegram("▶️ กลับมาทำงานตามปกติแล้ว (DCA bot และ trading bot)")
        elif re.match(r"^(เติมเงิน|ฝากเงิน|เติม|ฝาก)\s*[\d,]+(\.\d+)?\s*(บาท)?$", text):
            amount = float(re.sub(r"[^\d.]", "", text.replace(",", "")))
            if 0 < amount <= MAX_DEPOSIT_THB:
                inbox = load_json(DEPOSIT_INBOX_FILE, [])
                inbox.append({"amount_thb": amount, "at": datetime.now(timezone.utc).isoformat()})
                save_json(DEPOSIT_INBOX_FILE, inbox)
                send_telegram(
                    f"💵 รับทราบ เติมกองเงินบอท {amount:,.2f} บาท\n"
                    "บอทจะใช้ซื้อตามลำดับความเสี่ยงในรอบถัดไป (อย่าลืมโอนเงินเข้า Bitkub ให้ครบด้วย)"
                )
            else:
                send_telegram(f"⚠️ จำนวนเงินไม่ถูกต้อง (รับได้ 1 - {MAX_DEPOSIT_THB:,.0f} บาทต่อครั้ง)")
        elif text in CONFIRM_WORDS:
            confirm_seen = True
        elif text in CANCEL_WORDS:
            cancel_seen = True

    tg_state["offset"] = new_offset
    save_json(TG_OFFSET_FILE, tg_state)
    save_json(CONTROL_FILE, control)
    save_json(COMMANDS_FILE, {"confirm": confirm_seen, "cancel": cancel_seen})
    return control


def main():
    control = process_telegram_commands()

    # ---- ดึงราคา/ปริมาณซื้อขายของทุกคู่เทรดในครั้งเดียว ----
    response = requests.get("https://api.bitkub.com/api/market/ticker", timeout=20)
    ticker = response.json()

    trending = pick_trending_coins(ticker, TOP_N_TRENDING)
    active_names = [c["name"] for c in trending]
    # วิเคราะห์เหรียญที่ DCA ถืออยู่ด้วยเสมอ (แม้หลุดเทรนด์) เพื่อให้ตัดขาดทุนตามสัญญาณได้
    # (ไม่นับเป็น active_coins — ไม่ได้เพิ่มเข้าตัวเลือกซื้อ)
    held = load_json(DCA_STATE_FILE, {}).get("holdings", {})
    for name, h in held.items():
        if (h or {}).get("total_coin", 0) > 0 and name not in active_names and f"THB_{name}" in ticker:
            trending.append({"name": name, "ticker_symbol": f"THB_{name}", "candle_symbol": f"{name}_THB",
                             "quote_volume_thb": float(ticker[f"THB_{name}"].get("quoteVolume") or 0)})
    market_sentiment = fetch_market_sentiment()
    global_prices = bitkub_market.fetch_global_usdt_prices()  # ใช้คำนวณ premium ของ Bitkub เทียบตลาดโลก  # ดึงครั้งเดียวต่อรอบ ใช้ร่วมกันทุกเหรียญ (ดู fetch_market_sentiment)

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "active_coins": active_names,
        "control": control,
        "market_sentiment": round(market_sentiment, 3),
    }

    alert_state = load_json(ALERT_STATE_FILE, {})
    for coin in trending:
        name = coin["name"]
        change = ticker.get(coin["ticker_symbol"], {}).get("percentChange")
        if change is None:
            print(f"ข้าม {name}: ไม่พบข้อมูล ticker")
            continue
        check_change_alert(name, change, alert_state)
        try:
            trend = analyze_trend(name, coin["candle_symbol"], market_sentiment)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            print(f"ข้าม {name}: วิเคราะห์เทรนด์ไม่สำเร็จ — {e}")
            continue
        result[name] = {"change_24h": change, "quote_volume_thb": round(coin["quote_volume_thb"], 2), **trend}
        # ข้อมูลเฉพาะของ Bitkub (premium, ความหนากระดาน, แรงไล่ซื้อ) — เก็บไว้วัดผลก่อน ยังไม่ใช้ตัดสินใจ
        result[name].update(bitkub_market.market_features(name, ticker, global_prices))

    save_json(ALERT_STATE_FILE, alert_state)

    # ---- บันทึกผลล่าสุดไว้ให้หน้าเว็บ Dashboard อ่าน ----
    with open("latest.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("บันทึกไฟล์ latest.json แล้ว")
    print("เช็คราคาเสร็จสิ้น")


if __name__ == "__main__":
    main()
