# -*- coding: utf-8 -*-
"""
บอท DCA (Dollar-Cost Averaging) สำหรับ Bitkub
- ซื้อเหรียญที่กำหนดไว้ด้วยจำนวนเงินคงที่ "ทุกรอบ N วัน" (ไม่สน timing/สัญญาณใดๆ) — ดีฟอลต์ 7 วัน (รายสัปดาห์)
- ตัดปัญหา overfitting ของการจับจังหวะที่พิสูจน์แล้วว่าไม่มี edge จริงบน Bitkub
  (ดู backtest.py / filter_ablation — ทดสอบ 7 เหรียญ x 8 ชุดตัวกรอง ไม่มีชุดใดกำไรนอกช่วงข้อมูลที่ใช้หา params)
- รันผ่าน GitHub Actions ทุกชั่วโมงเหมือนบอทอื่น แต่จะ "ซื้อจริง" แค่ครั้งแรกของแต่ละรอบ
  (เช็คจาก state/dca_state.json ว่าผ่านมาครบ DCA_INTERVAL_DAYS วันจากครั้งล่าสุดหรือยัง — ไม่ผูกกับวันใดวันหนึ่ง
  ของสัปดาห์ จะเริ่มรอบแรกตอนไหนก็ได้ แล้วนับถัดไปทุก 7 วันจากนั้น)
- ผู้ใช้แจ้งว่ายอดเงินที่เติมเข้าบัญชีแต่ละรอบไม่สม่ำเสมอ — ถ้าเงินในบัญชีไม่พอซื้อครบทุกเหรียญ
  โค้ดจะสั่งซื้อแต่ละเหรียญแยกจากกัน (ไม่ใช่ all-or-nothing) เหรียญที่เงินพอจะซื้อได้ตามปกติ
  เหรียญที่เงินไม่พอจะล้มเหลวเฉพาะตัวนั้นและแจ้งใน Telegram โดยไม่กระทบเหรียญอื่น
- Take-profit อัตโนมัติ: เช็คทุกรอบว่าเหรียญไหน "กำไรเฉลี่ยรวม" (เทียบกับทุนเฉลี่ยที่ถือสะสมมา)
  เกิน TAKE_PROFIT_PCT หรือยัง ถ้าเกิน จะขายเหรียญนั้นทั้งหมดกลับเป็นเงินบาททันที แล้วเริ่มสะสม DCA
  ใหม่จากศูนย์สำหรับเหรียญนั้น (เหรียญอื่นที่ยังไม่ถึงเกณฑ์ไม่กระทบ)

สถานะทั้งหมดเก็บเป็นไฟล์ json ใต้โฟลเดอร์ state/ แล้วให้ GitHub Actions
commit กลับเข้า repo ทุกครั้งที่รัน (เพราะ Actions runner ใช้แล้วทิ้งไฟล์หายทุกรอบ)
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

import analysis_log
import trade_journal
from bitkub_trade import BitkubAPIError, fetch_fill, floor_amount, get_wallet, place_ask, place_bid

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BITKUB_API_KEY = os.environ.get("BITKUB_API_KEY", "").strip()
BITKUB_API_SECRET = os.environ.get("BITKUB_API_SECRET", "").strip()

BKK = timezone(timedelta(hours=7))

STATE_DIR = "state"
DCA_STATE_FILE = f"{STATE_DIR}/dca_state.json"
CONTROL_FILE = f"{STATE_DIR}/control.json"
DASHBOARD_FILE = "latest.json"

# ---------------- ตั้งค่า DCA (แก้ตัวเลขตรงนี้ได้เลยถ้าต้องการเปลี่ยน) ----------------
# เปลี่ยนจาก "ซื้อทุกเหรียญ คนละ 50 บาท ทุกสัปดาห์" (เดิม) เป็น "งบรวม 300 บาท/สัปดาห์
# ให้บอทเลือกเองว่าจะซื้อเหรียญไหนในงบนี้" ตามคำขอผู้ใช้ (ฝากเงินเข้าทุกสัปดาห์ สัปดาห์ละ 300 บาท)
#
# วิธีเลือก: เรียงเหรียญที่ "ติดเทรนด์" (active_coins) ตามคะแนนความเสี่ยงจากน้อยไปมาก แล้วซื้อไล่ลงมา
# ครั้งละ DCA_UNIT_THB บาท จนกว่าเงินในกองจะไม่พอ (เหรียญที่อยู่ท้ายแถวถ้าเงินไม่พอ จะไม่ได้ซื้อ
# สัปดาห์นั้น — ตามที่ผู้ใช้ต้องการ "ไล่ซื้อจากเสี่ยงน้อยสุดไปมากสุด อันดับสุดท้ายไม่ต้องซื้อถ้างบหมด")
#
# คะแนนความเสี่ยง = risk_pct (ความผันผวนราคาย้อนหลัง 48 ชม. ที่ crypto_alert.py คำนวณไว้)
# หักลบด้วยคะแนนข่าว/เซนติเมนต์ (news_sentiment จาก CryptoPanic, -1 ถึง +1) คูณน้ำหนัก
# ข่าวดี -> คะแนนความเสี่ยงลดลง (มีโอกาสถูกเลือกซื้อก่อน), ข่าวร้าย -> คะแนนเพิ่มขึ้น (ถูกเลื่อนไปท้ายแถว)
#
# ระบบกองเงินหมุนเวียน (ตามคำขอผู้ใช้ 17 ต.ค. 2569):
# - บอทใช้เงินจาก "กองเงินบอท" (cash_pool_thb ใน state) เท่านั้น ไม่แตะเงินบาทส่วนอื่นในบัญชี
# - กองเงินเพิ่มเมื่อผู้ใช้แจ้งเติมเงิน (พิมพ์ "เติม 300" ใน Telegram) — ไม่มีการเติมอัตโนมัติรายสัปดาห์
# - ขายทำกำไร (TP): ทุนเดิมกลับเข้ากองเพื่อซื้อเหรียญถัดไปทันที ส่วนกำไรแยกเก็บ (profit_reserved_thb) ไม่เอาไปซื้อต่อ
# - ตัดขาดทุน (SL): เงินที่ขายได้ทั้งหมดกลับเข้ากอง (ไม่มีกำไรให้แยก)
# - ซื้อไล่ตามลำดับความเสี่ยงน้อย -> มาก ครั้งละ DCA_UNIT_THB บาท จนเงินในกองไม่พอแล้วหยุด
# - จัดลำดับใหม่ทุก DCA_INTERVAL_DAYS วัน ในรอบเดียวกันเหรียญหนึ่งซื้อได้ครั้งเดียว
#   ยกเว้นเหรียญที่ขายไปแล้วในรอบนี้ ซื้อกลับได้ถ้าผ่านการประเมินความเสี่ยงตอนนั้น (ดู rebuy_check)
DCA_UNIT_THB = 200  # ทุนเป้าหมายต่อเหรียญ (ผู้ใช้เปลี่ยนจาก 50 เป็น 200 บาท 21 ต.ค. 2569) ถือน้อยกว่านี้ซื้อเพิ่มให้ครบ
MAX_PER_COIN_THB = 400  # ทุกเหรียญครบ 200 แล้วเงินยังเหลือ ซื้อซ้ำได้ไม่เกินเท่านี้ต่อเหรียญ
MIN_ORDER_THB = 20  # ไม่ส่งคำสั่งซื้อเล็กกว่านี้ (ขั้นต่ำ Bitkub 10 บาท เผื่อไว้)
DCA_INTERVAL_DAYS = 7
MUST_INCLUDE_COINS = ["BTC", "ETH"]
# เหรียญที่ขาย (TP/SL) ไปแล้วในรอบนี้ ซื้อกลับได้ในรอบเดียวกันถ้าประเมินความเสี่ยงตอนนั้นแล้วผ่าน:
# ความผันผวน 48 ชม. (risk_pct) ไม่เกินค่านี้ และแนวโน้ม MA ไม่ใช่ขาลง (Death Cross)
REBUY_MAX_RISK_PCT = 3.0
# ให้เหรียญที่แนวโน้มเป็น Golden Cross (MA12 > MA24 กราฟรายชั่วโมง) ได้ซื้อก่อน (ผู้ใช้ขอ 21 ต.ค. 2569)
# ไม่ได้ตัดเหรียญอื่นทิ้ง แค่เลื่อนขึ้นหน้าแถว — เหรียญ Golden Cross เรียงตามความเสี่ยงน้อย -> มากเหมือนเดิม
# ไม้ที่ซื้อเพราะ Golden Cross ถูกบันทึกใน journal (gc_priority) เพื่อเทียบ winrate กับวิธีเดิมภายหลัง
GOLDEN_CROSS_FIRST = True
# แจ้ง Telegram เมื่อกำไรที่แยกเก็บไว้ถึงยอดนี้ ให้ผู้ใช้กดถอนเองใน Bitkub (บอทไม่มีสิทธิ์ถอนเงิน — ปลอดภัยกว่า)
# ถอนแล้วพิมพ์ "ถอน 300" ใน Telegram เพื่อหักออกจากยอดกำไรแยกเก็บ
PROFIT_WITHDRAW_ALERT_THB = 300
WITHDRAW_ALERT_RESET_THB = 250  # แจ้งแล้ว ต้องลดลงต่ำกว่านี้ (เช่น ถอนออกไปแล้ว) ถึงจะแจ้งใหม่ได้
# ทั้งบัญชีเป็นกองเงินบอท (ผู้ใช้สั่ง 21 ต.ค. 2569): เงินบาทในบัญชีทั้งหมด = กองเงินบอท (อ่านจาก Bitkub ทุกรอบ)
# และทุกเหรียญในบัญชีอยู่ใต้กติกา TP/SL รวมถึงเหรียญที่ผู้ใช้ซื้อเองก่อนหน้า (ใช้ทุนจริงตาม EXTERNAL_HOLDINGS)
WHOLE_ACCOUNT_MODE = True
REINVEST_PROFIT = True  # ขายได้กำไร -> เงินทั้งหมดกลับเข้ากองไปซื้อต่อ (ไม่แยกเก็บกำไรแล้ว)
EXTERNAL_HOLDINGS = {  # เหรียญที่ผู้ใช้ซื้อเองนอกบอท (จากประวัติเทรดในแอป Bitkub ที่ผู้ใช้ส่งมา)
    "BTC": {"coin": 0.00005077, "cost_thb": 139.98, "bought": "2026-10-08", "price": 2757000.0},
    "ETH": {"coin": 0.00156855, "cost_thb": 124.70, "bought": "2026-10-15", "price": 79500.0},
}
# เว้นระยะซื้อคืน (ผู้ใช้เลือกทาง ค): หลังขายต้องรออย่างน้อยเท่านี้ และแนวโน้มต้องเป็นขาขึ้น (Golden Cross)
# กันการขายแล้วซื้อคืนวนซ้ำที่เสียค่าธรรมเนียมและโดนขายซ้ำ
REBUY_COOLDOWN_HOURS = 24
# error ที่แปลว่าเหรียญนี้ซื้อผ่าน API ไม่ได้ (ลองใหม่กี่ครั้งก็ไม่ผ่าน): 11 = symbol ไม่ถูกต้อง,
# 61 = เหรียญแบบ broker (source = broker) ที่ endpoint ซื้อขายไม่รองรับ
PERMANENT_BUY_ERRORS = (11, 61)  # อยู่ในตัวเลือกเสมอ แม้ไม่ติดอันดับปริมาณซื้อขาย
REBALANCE_FILE = f"{STATE_DIR}/rebalance_request.json"  # คำขอขายบางส่วน (ครั้งเดียว) เพื่อกระจายเหรียญ
DEPOSIT_INBOX_FILE = "state/deposit_inbox.json"  # crypto_alert.py เขียนยอดเติมเงินจาก Telegram ไว้ที่นี่
NEWS_SENTIMENT_WEIGHT = 5.0  # ข่าวบวก/ลบเต็มสเกล (+-1) ขยับคะแนนความเสี่ยงได้สูงสุด 5 แต้ม

# ขายอัตโนมัติ (ผู้ใช้เลือกให้ใช้ TP/SL ชุดเดียวกับบอทเทรดกับเหรียญ DCA แทนการเปิดไม้ใหม่ด้วยเงินบาท)
# เทียบราคาปัจจุบันกับทุนเฉลี่ยของแต่ละเหรียญ: กำไรถึง TAKE_PROFIT_PCT หรือขาดทุนถึง STOP_LOSS_PCT
# ขายเหรียญนั้นทั้งหมดกลับเป็นเงินบาท แล้วเริ่มสะสม DCA ใหม่จากศูนย์ (เหรียญอื่นไม่กระทบ)
TAKE_PROFIT_PCT = 5.0  # ผู้ใช้ปรับจาก 4% (20 ต.ค. 2569: "TP 5-6%")
STOP_LOSS_PCT = 15.0  # ผู้ใช้ปรับจาก 5% เป็น 15% (19 ต.ค. 2569)
# ตัดขาดทุนตามสัญญาณ (ผู้ใช้เลือกทางเลือกที่ 4): ขาดทุนตั้งแต่ CUT_ZONE_PCT ขึ้นไป และแนวโน้ม MA เป็นขาลง
# (Death Cross) -> ขายเลยไม่ต้องรอถึง STOP_LOSS_PCT / ถ้าแนวโน้มยังไม่ลง ถือรอ (STOP_LOSS_PCT ยังเป็นเพดานเสมอ)
CUT_ZONE_PCT = 3.0
# ปิดการตัดขาดทุนตามสัญญาณ (ผู้ใช้เลือกทาง ก, 20 ต.ค. 2569): ผลทบทวน 17-19 ต.ค. พบว่ากฎนี้ขายตรงก้น
# ตอนตลาดลงทั้งกระดานแล้วเด้งกลับ (15 จาก 17 ไม้ที่แพ้) — เหลือแค่ TP 4% และเพดาน SL 15%
SIGNAL_CUT_ENABLED = False

# ส่งสรุปพอร์ตรวมทาง Telegram ทุกๆ กี่วัน (รายสัปดาห์)
SUMMARY_INTERVAL_DAYS = 7

# แจ้งเตือนครั้งเดียวถ้าพอร์ตรวม (unrealized) ขาดทุนเกินระดับนี้ — ไม่ใช่เงื่อนไขหยุดซื้อ
# (DCA ยังคงซื้อต่อตามปกติ แค่แจ้งให้รู้) และจะไม่แจ้งซ้ำจนกว่าจะฟื้นกลับมาเกิน CRASH_RECOVERY_THRESHOLD_PCT ก่อน
CRASH_ALERT_THRESHOLD_PCT = -15.0
CRASH_RECOVERY_THRESHOLD_PCT = -5.0

# รายชื่อเหรียญตั้งต้น (ใช้ตอนยังไม่เคยมี state มาก่อน) — หลังจากนั้นรายชื่อเหรียญ "ที่กำลังซื้ออยู่" จะถูกปรับ
# อัตโนมัติทุกรอบตามเหรียญติดเทรนด์ล่าสุด (ดู active_coins ที่ crypto_alert.py คำนวณไว้ใน latest.json)
# เหรียญที่เคยซื้อสะสมไว้แล้วแต่หลุดเทรนด์จะ "ไม่ซื้อเพิ่ม" แต่ยังถือของเดิมรอ TP/SL ตามปกติ
DEFAULT_SEED_COINS = ["BTC", "ETH", "XRP", "ADA", "DOGE", "SOL", "KUB"]


def trade_symbol_of(name):
    # ใช้รูปแบบตัวพิมพ์ใหญ่ "THB_BTC" สำหรับ lookup ใน ticker/candle ของ Bitkub (ticker API คืนคีย์แบบนี้)
    return f"THB_{name}"


def order_symbol_of(trade_symbol):
    """
    แปลง "THB_BTC" (คีย์ของ ticker) เป็น "btc_thb" สำหรับ place-bid/place-ask
    ตาม /api/v3/market/symbols ที่ใช้รูปแบบ เหรียญ_THB และคำอธิบาย param ในเอกสาร ("e.g. btc_thb")
    """
    quote, base = trade_symbol.split("_", 1)
    return f"{base}_{quote}".lower()


# ---------------- ฟังก์ชันพื้นฐาน ----------------

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


def get_ticker():
    """ดึงราคาล่าสุดของทุกคู่เทรดในครั้งเดียว (public API ไม่ต้อง sign)"""
    response = requests.get("https://api.bitkub.com/api/market/ticker", timeout=20)
    return response.json()


def today_str():
    return datetime.now(BKK).strftime("%Y-%m-%d")


def days_since(date_str, today):
    d1 = datetime.strptime(date_str, "%Y-%m-%d")
    d2 = datetime.strptime(today, "%Y-%m-%d")
    return (d2 - d1).days


def default_state():
    return {
        "holdings": {
            name: {
                "total_invested_thb": 0.0,
                "total_coin": 0.0,
                "buy_count": 0,
                "last_bought_date": None,
            }
            for name in DEFAULT_SEED_COINS
        },
        "log": [],
        "sell_log": [],
        "realized_pnl_total_thb": 0.0,
        "dca_cycle": None,  # รอบจัดลำดับปัจจุบัน (ดู build_new_cycle)
        "cash_pool_thb": 0.0,
        "profit_reserved_thb": 0.0,
    }


def risk_score(risk_pct, news_sentiment):
    """คะแนนยิ่งน้อย = ยิ่งถูกเลือกซื้อก่อน (ความเสี่ยงต่ำ + ข่าวดี)"""
    return risk_pct - NEWS_SENTIMENT_WEIGHT * (news_sentiment or 0.0)


def apply_inbox(state):
    """
    ประมวลผลข้อความเงินเข้า/ออกจาก Telegram (state/deposit_inbox.json)
    - เติม X -> เพิ่มทุน (capital_thb) — ตัวเงินบาทบอทอ่านจากบัญชีเอง ไม่ต้องบวกกองเงินซ้ำ
    - ถอน X -> บันทึกยอดถอน (ถ้าถอนเกินกำไรที่มี ส่วนเกินถือเป็นการถอนทุน หักออกจากทุน)
    คืน True ถ้ามีรายการ
    """
    inbox = load_json(DEPOSIT_INBOX_FILE, [])
    if not inbox:
        return False
    for d in inbox:
        amount = float(d.get("amount_thb") or 0)
        if d.get("kind") == "withdraw":
            profit_before = max((state.get("last_equity_thb") or 0) - state.get("capital_thb", 0.0), 0.0)
            from_capital = round(max(amount - profit_before, 0.0), 2)
            state["capital_thb"] = round(state.get("capital_thb", 0.0) - from_capital, 2)
            state["withdrawn_total_thb"] = round(state.get("withdrawn_total_thb", 0.0) + amount, 2)
            state.setdefault("withdraw_log", []).append({"at": d.get("at"), "amount_thb": amount,
                                                          "from_capital_thb": from_capital})
            analysis_log.event("withdraw", None, amount_thb=amount, from_capital_thb=from_capital,
                               capital_after=state["capital_thb"])
            note = f"\n(เกินกำไรที่มี {from_capital:,.2f} บาท นับเป็นการถอนทุน)" if from_capital > 0 else ""
            send_telegram(f"🏦 บันทึกการถอน {amount:,.2f} บาทแล้ว{note}\n"
                          f"ทุนคงเหลือ {state['capital_thb']:,.2f} บาท · ถอนสะสม {state['withdrawn_total_thb']:,.2f} บาท")
        else:
            state["capital_thb"] = round(state.get("capital_thb", 0.0) + amount, 2)
            if not WHOLE_ACCOUNT_MODE:
                state["cash_pool_thb"] = round(state.get("cash_pool_thb", 0.0) + amount, 2)
            print(f"เติมทุน {amount:,.2f} บาท -> ทุนรวม {state['capital_thb']:,.2f} บาท")
    save_json(DEPOSIT_INBOX_FILE, [])
    return True


def migrate_whole_account(state, wallet):
    """ครั้งแรกที่เปิดโหมดทั้งบัญชี: รวมเหรียญที่ผู้ใช้ซื้อเอง + ย้ายกำไรแยกเก็บกลับเข้ากอง + ตั้งทุนตั้งต้น"""
    journal = load_journal(state)
    for name, ext in EXTERNAL_HOLDINGS.items():
        h = state["holdings"].setdefault(
            name, {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0, "last_bought_date": None})
        h["total_invested_thb"] = round(h["total_invested_thb"] + ext["cost_thb"], 4)
        h["total_coin"] = h["total_coin"] + ext["coin"]
        h["buy_count"] += 1
        journal["lots"].append({
            "lot_id": f"external-{name}-{ext['bought']}", "coin": name, "opened_at": ext["bought"],
            "invested_thb": ext["cost_thb"], "fill_price": ext["price"], "amount_coin": ext["coin"],
            "features": None, "note": "ผู้ใช้ซื้อเองก่อนรวมทั้งบัญชีเข้าบอท", "status": "open",
        })
    trade_journal.save(journal)
    state["profit_reserved_moved_thb"] = state.get("profit_reserved_thb", 0.0)
    state["profit_reserved_thb"] = 0.0
    state["whole_account_since"] = datetime.now(timezone.utc).isoformat()
    _sync_from_wallet(state, wallet)
    invested = sum(h["total_invested_thb"] for h in state["holdings"].values() if h.get("total_coin", 0) > 0)
    state["capital_thb"] = round(state["cash_pool_thb"] + invested, 2)
    analysis_log.event("whole_account_start", None, capital_thb=state["capital_thb"],
                       cash_thb=state["cash_pool_thb"], invested_thb=round(invested, 2))
    send_telegram(
        "🔄 เปลี่ยนเป็นโหมดทั้งบัญชีแล้ว — เงินบาทและทุกเหรียญในบัญชีเป็นกองเงินบอท\n"
        f"ทุนตั้งต้น {state['capital_thb']:,.2f} บาท (เงินบาท {state['cash_pool_thb']:,.2f} + เหรียญตามทุน {invested:,.2f})\n"
        f"ซื้อเหรียญละ {DCA_UNIT_THB} บาท ตามลำดับ Golden Cross → ความเสี่ยงน้อย กำไรทบลงทุนต่อ\n"
        f"เติมเงินแล้วพิมพ์ \"เติม X\" / ถอนแล้วพิมพ์ \"ถอน X\" เพื่อให้นับทุนถูก"
    )


def _sync_from_wallet(state, wallet):
    state["cash_pool_thb"] = round(wallet["THB"]["available"], 2)
    pending = {po["name"] for po in state.get("pending_orders", [])}
    for name, h in state["holdings"].items():
        if h.get("total_coin", 0) > 0 and name not in pending and name in wallet:
            h["total_coin"] = wallet[name]["total"]
            (state.get("dust") or {}).pop(name, None)  # เศษรวมอยู่ในยอดจริงแล้ว
            if h["total_coin"] <= 0:  # เหรียญหายจากบัญชี (เช่น ผู้ใช้ขายเอง) — เริ่มนับทุนใหม่
                analysis_log.event("holding_gone", name, invested_thb=h["total_invested_thb"])
                h["total_invested_thb"] = 0.0


def sync_wallet(state):
    """โหมดทั้งบัญชี: กองเงิน = เงินบาทจริงในบัญชี, จำนวนเหรียญ = ยอดจริง (เรียกหลังยืนยันคำสั่งที่ค้าง)"""
    if not WHOLE_ACCOUNT_MODE:
        return False
    wallet = get_wallet(BITKUB_API_KEY, BITKUB_API_SECRET)
    if not wallet:
        print("อ่านยอดในบัญชีไม่สำเร็จ — ใช้ยอดเดิมที่บันทึกไว้")
        return False
    if not state.get("whole_account_since"):
        migrate_whole_account(state, wallet)
    else:
        _sync_from_wallet(state, wallet)
    return True


def update_equity(state, ticker):
    """มูลค่าพอร์ตรวม (เงินบาท + เหรียญ) เทียบทุน แล้วแจ้งเมื่อกำไรเกิน PROFIT_WITHDRAW_ALERT_THB"""
    if not isinstance(ticker, dict) or not ticker:
        return
    value = 0.0
    for name, h in state["holdings"].items():
        if h.get("total_coin", 0) > 0:
            t = ticker.get(trade_symbol_of(name)) or {}
            value += h["total_coin"] * float(t.get("last") or 0)
    equity = round(state.get("cash_pool_thb", 0.0) + value, 2)
    state["last_equity_thb"] = equity
    if not state.get("capital_thb"):
        return
    profit = equity - state["capital_thb"]
    if profit >= PROFIT_WITHDRAW_ALERT_THB and not state.get("withdraw_alert_sent"):
        send_telegram(
            f"💰 พอร์ตโตเกินทุนแล้ว {profit:,.2f} บาท (ทุน {state['capital_thb']:,.2f} → ตอนนี้ {equity:,.2f} บาท)\n"
            "ถ้าจะถอนกำไร ถอนเข้าบัญชีธนาคารในแอป Bitkub ได้เลย (บอทไม่ถอนเงินเอง)\n"
            f"ถอนแล้วพิมพ์ \"ถอน {int(min(profit, PROFIT_WITHDRAW_ALERT_THB))}\" ให้บอทบันทึก"
        )
        state["withdraw_alert_sent"] = True
    elif profit < WITHDRAW_ALERT_RESET_THB and state.get("withdraw_alert_sent"):
        state["withdraw_alert_sent"] = False


def is_golden_cross(name, dashboard):
    info = (dashboard or {}).get(name) if isinstance(dashboard, dict) else None
    return "Golden Cross" in str((info or {}).get("signal", ""))


def buy_order(ranking, dashboard):
    """ลำดับซื้อจริง: เหรียญ Golden Cross ขึ้นก่อน (ถ้าเปิด GOLDEN_CROSS_FIRST) ที่เหลือตามลำดับความเสี่ยงเดิม"""
    if not GOLDEN_CROSS_FIRST:
        return list(ranking)
    gc = [n for n in ranking if is_golden_cross(n, dashboard)]
    return gc + [n for n in ranking if n not in gc]


def build_new_cycle(today, active_coins, dashboard_so_far):
    """
    เริ่มรอบงบรายสัปดาห์ใหม่: เรียงลำดับเหรียญที่จะซื้อตามคะแนนความเสี่ยง (น้อย -> มาก)
    ล็อกลำดับไว้ทั้งรอบ (ไม่คำนวณใหม่ทุกชั่วโมง) เพื่อให้การซื้อแบบ retry รายชั่วโมงสอดคล้องกันทั้งสัปดาห์
    ถ้าเหรียญไหนยังไม่มีข้อมูล risk_pct (เช่น เพิ่งเพิ่มฟีเจอร์ หรือดึงแท่งเทียนไม่สำเร็จรอบก่อน)
    จะต่อท้ายแถวตามลำดับเดิมใน active_coins แทน (ไม่ตัดออก กันบอทหยุดซื้อเฉยๆ เพราะข้อมูลขาดชั่วคราว)
    """
    candidates = list(active_coins) + [c for c in MUST_INCLUDE_COINS if c not in active_coins]
    ranked, unranked = [], []
    for name in candidates:
        info = dashboard_so_far.get(name, {}) if isinstance(dashboard_so_far, dict) else {}
        risk_pct = info.get("risk_pct")
        if risk_pct is None:
            unranked.append(name)
        else:
            ranked.append((risk_score(risk_pct, info.get("news_sentiment")), name))
    ranked.sort(key=lambda x: x[0])
    ranking = [name for _, name in ranked] + unranked
    return {
        "cycle_date": today,
        "ranking": ranking,
        "bought": {},
    }


# ---------------- ฟังก์ชันสั่งซื้อจริง ----------------

def execute_buy(trade_symbol, amount_thb):
    """
    สั่งซื้อจริงด้วยเงิน amount_thb บาท (market order)
    ผลตอบกลับของ market order มี rat/rec = 0 เสมอ (ยังไม่รู้ราคาจริงตอนตอบ) จึงถือว่าสำเร็จเมื่อได้ order id
    แล้วถาม order-info ต่อเพื่อเอาราคา/จำนวนเหรียญที่ได้จริง ถ้ายังไม่ได้ข้อมูล คืน pending=True ให้ไปเช็ครอบหน้า
    """
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        return {"ok": False, "error": "ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET"}
    sym = order_symbol_of(trade_symbol)
    try:
        result = place_bid(BITKUB_API_KEY, BITKUB_API_SECRET, sym, amount_thb, rat=0, typ="market")
    except (BitkubAPIError, requests.RequestException) as e:
        return {"ok": False, "error": str(e)}

    res = result.get("result", result)
    if result.get("error", 0) not in (0, None) or not isinstance(res, dict) or not res.get("id"):
        return {"ok": False, "error": f"Bitkub error code {result.get('error')}: {res}"}

    order_id = str(res["id"])
    fill, raw = fetch_fill(BITKUB_API_KEY, BITKUB_API_SECRET, sym, order_id, "buy")
    if not fill:
        return {"ok": True, "pending": True, "order_id": order_id, "sym": sym, "raw": raw}
    return {"ok": True, "pending": False, "order_id": order_id, "sym": sym,
            "fill_price": fill["rate"], "amount_coin": fill["coin"]}


def execute_sell(trade_symbol, amount_coin, fallback_price=None):
    """
    สั่งขายจริงทั้งหมดที่ถืออยู่ของเหรียญนี้ (market order) กลับเป็นเงินบาท
    จำนวนปัดเศษลงที่ทศนิยม 8 ตำแหน่ง ถ้ายังโดน error 18 (เหรียญไม่พอ) ลองใหม่ที่ 99.9%
    คืน sold_coin = จำนวนที่ขายได้จริง (ใช้คำนวณเศษที่เหลือ)
    """
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        return {"ok": False, "error": "ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET"}
    sym = order_symbol_of(trade_symbol)
    result, res = None, None
    amt = 0.0
    for factor in (1.0, 0.999):
        amt = floor_amount(amount_coin * factor)
        try:
            result = place_ask(BITKUB_API_KEY, BITKUB_API_SECRET, sym, amt, rat=0, typ="market")
        except (BitkubAPIError, requests.RequestException) as e:
            return {"ok": False, "error": str(e)}
        res = result.get("result", result)
        if result.get("error") == 18 and factor == 1.0:
            continue
        break

    if result.get("error", 0) not in (0, None) or not isinstance(res, dict) or not res.get("id"):
        return {"ok": False, "error": f"Bitkub error code {result.get('error')}: {res}"}

    order_id = str(res["id"])
    fill, _raw = fetch_fill(BITKUB_API_KEY, BITKUB_API_SECRET, sym, order_id, "sell")
    if fill:
        return {"ok": True, "order_id": order_id, "sell_price": fill["rate"], "proceeds_thb": fill["thb"], "sold_coin": amt}
    if fallback_price:
        # ขายไปแล้วจริงแต่ยังอ่านผลไม่ได้ — ประมาณจากราคาตลาดล่าสุด (ไม่หักค่าธรรมเนียม)
        return {"ok": True, "order_id": order_id, "sell_price": fallback_price,
                "proceeds_thb": amt * fallback_price, "estimated": True, "sold_coin": amt}
    return {"ok": True, "order_id": order_id, "sell_price": 0.0, "proceeds_thb": 0.0, "estimated": True, "sold_coin": amt}


def load_journal(state):
    journal = trade_journal.load()
    trade_journal.backfill_from_state(journal, state)
    return journal


def reconcile_pending_orders(state, today):
    """
    เช็คคำสั่งซื้อที่ส่งไปแล้วแต่ยังไม่รู้ผลจริง (pending_orders) ผ่าน order-info
    ถ้าจับคู่แล้ว: เพิ่มจำนวนเหรียญเข้า holdings + บันทึก log แล้วเอาออกจาก pending
    (ทุนที่ลงไป total_invested_thb ถูกบันทึกไว้แล้วตั้งแต่ตอนส่งคำสั่ง)
    """
    pending = state.get("pending_orders") or []
    still = []
    for po in pending:
        fill, raw = fetch_fill(BITKUB_API_KEY, BITKUB_API_SECRET, po["sym"], po["order_id"], po.get("side", "buy"), tries=2)
        if not fill:
            po["checks"] = po.get("checks", 0) + 1
            po["last_raw"] = raw
            still.append(po)
            continue
        name = po["name"]
        h = state["holdings"].setdefault(
            name, {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0, "last_bought_date": None}
        )
        h["total_coin"] = h["total_coin"] + fill["coin"]
        journal = load_journal(state)
        trade_journal.record_fill(journal, po["order_id"], fill["rate"], fill["coin"])
        trade_journal.save(journal)
        state["log"].append({
            "date": po.get("date", today),
            "name": name,
            "invested_thb": po.get("amt_thb"),
            "fill_price": fill["rate"],
            "amount_coin": fill["coin"],
            "order_id": po["order_id"],
        })
        print(f"ยืนยันคำสั่งซื้อ {name} ({po['order_id']}): {fill['coin']:.8f} เหรียญ @ {fill['rate']}")
    state["pending_orders"] = still
    return [po["name"] for po in pending if po not in still]


def run_rebalance(state, ticker, today):
    """
    ขายบางส่วนตามคำขอใน REBALANCE_FILE แล้วลบคำขอทิ้ง (ทำครั้งเดียว)
    รูปแบบ: {"coins": {"DOGE": {"reduce_thb": 200, "lot_id": "..."}}, "reason": "..."}
    ลดทุนของเหรียญตามสัดส่วน, ปิดไม้ใน journal ที่ระบุ, เงินที่ได้กลับเข้ากองไปซื้อเหรียญอื่นตามลำดับ
    """
    req = load_json(REBALANCE_FILE, None)
    if not req or not req.get("coins"):
        return
    remaining, lines = {}, []
    for name, spec in req["coins"].items():
        h = state["holdings"].get(name) or {}
        reduce_thb = float(spec.get("reduce_thb") or 0)
        if h.get("total_coin", 0) <= 0 or h.get("total_invested_thb", 0) <= reduce_thb or reduce_thb <= 0:
            continue
        if any(po["name"] == name for po in state.get("pending_orders", [])):
            remaining[name] = spec
            continue
        frac = reduce_thb / h["total_invested_thb"]
        tick = ticker.get(trade_symbol_of(name)) if isinstance(ticker, dict) else None
        price = (tick or {}).get("last")
        outcome = execute_sell(trade_symbol_of(name), h["total_coin"] * frac, fallback_price=price)
        if not outcome["ok"]:
            remaining[name] = spec
            print(f"{name}: ขายปรับสัดส่วนไม่สำเร็จ — {outcome['error']} (ลองใหม่รอบหน้า)")
            continue
        sold = outcome.get("sold_coin", h["total_coin"] * frac)
        h["total_coin"] = max(h["total_coin"] - sold, 0.0)
        h["total_invested_thb"] = round(h["total_invested_thb"] - reduce_thb, 4)
        pnl = outcome["proceeds_thb"] - reduce_thb
        state["cash_pool_thb"] = round(state.get("cash_pool_thb", 0.0) + outcome["proceeds_thb"], 2)
        state["realized_pnl_total_thb"] = round(state.get("realized_pnl_total_thb", 0.0) + pnl, 2)
        state["sell_log"].append({"date": today, "name": name, "sold_coin": sold, "sell_price": outcome["sell_price"],
                                  "invested_thb": reduce_thb, "proceeds_thb": round(outcome["proceeds_thb"], 2),
                                  "realized_pnl_thb": round(pnl, 2), "reason": "rebalance"})
        analysis_log.event("sell", name, reason="rebalance", proceeds_thb=round(outcome["proceeds_thb"], 4),
                           invested_thb=reduce_thb, sell_price=outcome["sell_price"])
        journal = load_journal(state)
        for lot in journal["lots"]:
            if lot.get("lot_id") == spec.get("lot_id") and lot["status"] == "open":
                lot.update({"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat(),
                            "exit_reason": "rebalance", "exit_price": outcome["sell_price"],
                            "proceeds_thb": round(outcome["proceeds_thb"], 4), "pnl_thb": round(pnl, 4),
                            "pnl_pct": round(pnl / reduce_thb * 100, 3), "win": pnl > 0,
                            "position_pnl_pct_at_trigger": None})
        trade_journal.save(journal)
        lines.append(f"  {name}: ขาย {reduce_thb:.0f} บาทของทุน → ได้ {outcome['proceeds_thb']:.2f} บาท ({pnl:+.2f})")
    if lines:
        send_telegram("⚖️ ปรับสัดส่วน — ขายส่วนเกินเพื่อกระจายไปเหรียญอื่น\n" + "\n".join(lines)
                      + f"\nกองเงินบอท {state['cash_pool_thb']:,.2f} บาท → ซื้อเหรียญถัดไปตามลำดับ")
    if remaining:
        save_json(REBALANCE_FILE, {**req, "coins": remaining})
    elif os.path.exists(REBALANCE_FILE):
        os.remove(REBALANCE_FILE)


def run_take_profit_stop_loss(state, ticker, today, dashboard_so_far=None):
    """
    เช็คทุกเหรียญที่ถืออยู่ (รวมเหรียญที่หลุดเทรนด์) เทียบกับทุนเฉลี่ย แล้วขายทั้งหมดเมื่อ:
    - กำไรถึง TAKE_PROFIT_PCT (take_profit)
    - ขาดทุนถึง STOP_LOSS_PCT (stop_loss — เพดาน)
    - ขาดทุนตั้งแต่ CUT_ZONE_PCT และแนวโน้มเป็น Death Cross (signal_cut)
    แล้วคืนเงินเข้ากองตามกติกา (ดูหัวไฟล์)
    """
    dashboard_so_far = dashboard_so_far or {}
    for name in list(state["holdings"].keys()):
        h = state["holdings"][name]
        if h["total_coin"] <= 0:
            continue
        if any(po["name"] == name for po in state["pending_orders"]):
            continue  # ยังมีคำสั่งซื้อที่รอยืนยัน จำนวนเหรียญ/ทุนเฉลี่ยยังไม่ครบ อย่าเพิ่งขาย
        tick = ticker.get(trade_symbol_of(name)) if isinstance(ticker, dict) else None
        current_price = tick.get("last") if isinstance(tick, dict) else None
        if not current_price:
            continue
        avg_cost = h["total_invested_thb"] / h["total_coin"] if h["total_coin"] else None
        if not avg_cost:
            continue
        pnl_pct_now = (current_price - avg_cost) / avg_cost * 100
        signal = str((dashboard_so_far.get(name) or {}).get("signal", "")) if isinstance(dashboard_so_far, dict) else ""
        if pnl_pct_now >= TAKE_PROFIT_PCT:
            reason = "take_profit"
        elif pnl_pct_now <= -STOP_LOSS_PCT:
            reason = "stop_loss"
        elif SIGNAL_CUT_ENABLED and pnl_pct_now <= -CUT_ZONE_PCT and "Death Cross" in signal:
            reason = "signal_cut"
        else:
            if SIGNAL_CUT_ENABLED and pnl_pct_now <= -CUT_ZONE_PCT:
                print(f"{name}: ขาดทุน {pnl_pct_now:.1f}% แต่แนวโน้มยังไม่ใช่ขาลง ({signal or 'ไม่มีข้อมูล'}) — ถือรอ")
            if pnl_pct_now < 0:
                analysis_log.event("hold_in_loss", name, pnl_pct=round(pnl_pct_now, 3), signal=signal)
            continue
        is_stop_loss = reason != "take_profit"

        # ขายเศษที่ค้างจากการขายครั้งก่อนๆ (state["dust"]) รวมไปด้วย
        dust = (state.get("dust") or {}).get(name, 0.0)
        to_sell = h["total_coin"] + dust
        outcome = execute_sell(trade_symbol_of(name), to_sell, fallback_price=current_price)
        if outcome["ok"]:
            leftover = max(to_sell - outcome.get("sold_coin", to_sell), 0.0)
            state.setdefault("dust", {})[name] = round(leftover, 8)
            invested = h["total_invested_thb"]
            realized_pnl = outcome["proceeds_thb"] - invested
            if REINVEST_PROFIT:
                back_to_pool = outcome["proceeds_thb"]  # กำไรทบลงทุนต่อทั้งหมด
            elif realized_pnl > 0:
                # กำไรแยกเก็บ ทุนเดิมกลับเข้ากองไปซื้อเหรียญถัดไป
                state["profit_reserved_thb"] = round(state.get("profit_reserved_thb", 0.0) + realized_pnl, 2)
                back_to_pool = invested
            else:
                back_to_pool = outcome["proceeds_thb"]
            state["cash_pool_thb"] = round(state.get("cash_pool_thb", 0.0) + back_to_pool, 2)
            cycle = state.get("dca_cycle")
            if cycle:
                cycle["bought"].pop(name, None)  # ปลดล็อกให้ซื้อกลับได้ ถ้าผ่าน rebuy_check
                cycle.setdefault("sold", {})[name] = {"reason": reason, "at": datetime.now(timezone.utc).isoformat()}
            state["realized_pnl_total_thb"] = round(state.get("realized_pnl_total_thb", 0.0) + realized_pnl, 2)
            analysis_log.event("sell", name, reason=reason, pnl_pct=round(pnl_pct_now, 3),
                               proceeds_thb=round(outcome["proceeds_thb"], 4), invested_thb=invested,
                               sell_price=outcome["sell_price"], back_to_pool=round(back_to_pool, 4))
            journal = load_journal(state)
            trade_journal.record_close(journal, name, outcome["sell_price"], outcome["proceeds_thb"], reason, pnl_pct_now)
            trade_journal.save(journal)
            state["sell_log"].append({
                "date": today,
                "name": name,
                "sold_coin": h["total_coin"],
                "sell_price": outcome["sell_price"],
                "invested_thb": h["total_invested_thb"],
                "proceeds_thb": round(outcome["proceeds_thb"], 2),
                "realized_pnl_thb": round(realized_pnl, 2),
                "reason": reason,
            })
            state["sell_log"] = state["sell_log"][-20000:]
            if reason == "signal_cut":
                head = f"📉 ตัดขาดทุน {name} ทั้งหมดตามสัญญาณขาลง (Death Cross) ที่ {pnl_pct_now:.1f}%"
            elif is_stop_loss:
                head = f"🛑 ตัดขาดทุน {name} ทั้งหมด (ขาดทุนเฉลี่ยรวมถึง {pnl_pct_now:.1f}%)"
            else:
                head = f"🎉 ขายทำกำไร {name} ทั้งหมด (กำไรเฉลี่ยรวมถึง +{pnl_pct_now:.1f}%)"
            send_telegram(
                f"{head}\n"
                f"ทุนสะสม {h['total_invested_thb']:.2f} บาท → ได้คืน {outcome['proceeds_thb']:.2f} บาท "
                f"({realized_pnl:+.2f} บาท)\n"
                + (f"แยกกำไร {realized_pnl:.2f} บาทเก็บไว้ (กำไรสะสม {state['profit_reserved_thb']:.2f} บาท)\n"
                   if realized_pnl > 0 and not REINVEST_PROFIT else "")
                + f"คืนเข้ากองซื้อ {back_to_pool:.2f} บาท → กองเงินบอทมี {state['cash_pool_thb']:.2f} บาท"
            )
            # เริ่มสะสมใหม่จากศูนย์สำหรับเหรียญนี้ (เหรียญอื่นไม่กระทบ)
            h["total_invested_thb"] = 0.0
            h["total_coin"] = 0.0
            h["buy_count"] = 0
            h["last_bought_date"] = None
        else:
            print(f"{name}: ถึงเกณฑ์ขาย ({pnl_pct_now:+.1f}%) แต่ขายไม่สำเร็จ — {outcome['error']} (ลองใหม่รอบหน้า)")


def rebuy_check(name, dashboard_so_far, sold_at=None):
    """ประเมินว่าเหรียญที่เพิ่งขายไป ควรซื้อกลับตอนนี้ไหม คืน (ok, เหตุผล)"""
    info = dashboard_so_far.get(name, {}) if isinstance(dashboard_so_far, dict) else {}
    analysed_at = dashboard_so_far.get("updated_at") if isinstance(dashboard_so_far, dict) else None
    if sold_at and (not analysed_at or analysed_at <= sold_at):
        # ข้อมูลความเสี่ยง/แนวโน้มยังเป็นของก่อนขาย (เช่นขายกลางชั่วโมงตอนราคาร่วง) — รอผลวิเคราะห์รอบใหม่
        return False, "รอผลวิเคราะห์รอบใหม่หลังขาย"
    risk_pct = info.get("risk_pct")
    if risk_pct is None:
        return False, "ไม่มีข้อมูลความเสี่ยงล่าสุด"
    if sold_at:
        hours = (datetime.now(timezone.utc) - datetime.fromisoformat(sold_at)).total_seconds() / 3600
        if hours < REBUY_COOLDOWN_HOURS:
            return False, f"เพิ่งขายไป {hours:.0f} ชม. (ต้องรอครบ {REBUY_COOLDOWN_HOURS} ชม.)"
    if risk_pct > REBUY_MAX_RISK_PCT:
        return False, f"ผันผวน {risk_pct:.2f}% เกิน {REBUY_MAX_RISK_PCT}%"
    if "Golden Cross" not in str(info.get("signal", "")):
        return False, "แนวโน้มยังไม่เป็นขาขึ้น (ต้อง Golden Cross)"
    return True, f"ผันผวน {risk_pct:.2f}% แนวโน้มขาขึ้น"


def _buy_one(state, cycle, name, amt, today, dashboard_so_far, gc_priority, bought_lines, failed_lines):
    """ซื้อเหรียญเดียวจำนวน amt บาท คืน "ok" / "skip" (ไปเหรียญถัดไป) / "stop" (เงินในบัญชีไม่พอ หยุดรอบนี้)"""
    outcome = execute_buy(trade_symbol_of(name), amt)
    if outcome["ok"]:
        analysis_log.event("buy", name, thb=amt, order_id=outcome["order_id"],
                           fill_price=outcome.get("fill_price"), rebuy=name in cycle.get("sold", {}), gc_priority=gc_priority,
                           pool_after=round(state["cash_pool_thb"] - amt, 2))
        journal = load_journal(state)
        trade_journal.record_buy(
            journal, name, outcome["order_id"], amt,
            outcome.get("fill_price"), outcome.get("amount_coin"), dashboard_so_far,
            rebuy=name in cycle.get("sold", {}), gc_priority=gc_priority,
        )
        trade_journal.save(journal)
        h = state["holdings"].setdefault(
            name, {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0, "last_bought_date": None}
        )
        h["total_invested_thb"] = round(h["total_invested_thb"] + amt, 4)
        h["buy_count"] = h["buy_count"] + 1
        h["last_bought_date"] = today
        if outcome.get("pending"):
            # Bitkub รับคำสั่งแล้ว แต่ยังอ่านราคาจริงไม่ได้ — เก็บไว้ยืนยันรอบหน้า (ไม่ซื้อซ้ำ)
            state["pending_orders"].append({
                "name": name, "sym": outcome["sym"], "order_id": outcome["order_id"],
                "side": "buy", "amt_thb": amt, "date": today,
            })
            bought_lines.append(f"  {name}: {amt} บาท (รอยืนยันราคา)")
        else:
            h["total_coin"] = h["total_coin"] + outcome["amount_coin"]
            state["log"].append({
                "date": today,
                "name": name,
                "invested_thb": amt,
                "fill_price": outcome["fill_price"],
                "amount_coin": outcome["amount_coin"],
                "order_id": outcome["order_id"],
            })
            bought_lines.append(f"  {name}: {amt} บาท @ {outcome['fill_price']:,.4f}")
        if gc_priority:
            bought_lines[-1] += " 📈 Golden Cross"
        if cycle.get("sold", {}).pop(name, None):
            bought_lines[-1] += " (ซื้อกลับ — ผ่านการประเมินความเสี่ยง)"
        cycle["bought"][name] = True
        state["cash_pool_thb"] = round(state["cash_pool_thb"] - amt, 2)
        state.get("last_errors", {}).pop(name, None)
        return "ok"
    else:
        state["last_errors"] = state.get("last_errors", {})
        prev = (state["last_errors"].get(name) or {}).get("error")
        if prev != outcome["error"]:
            failed_lines.append(f"  {name}: ล้มเหลว — {outcome['error']}")  # แจ้งเฉพาะ error ใหม่ ไม่แจ้งซ้ำทุกนาที
        state["last_errors"][name] = {"error": outcome["error"], "at": datetime.now(timezone.utc).isoformat()}
        analysis_log.event("buy_failed", name, error=outcome["error"])
        if any(f"error code {c}:" in outcome["error"] for c in PERMANENT_BUY_ERRORS):
            # ซื้อผ่าน API ไม่ได้ถาวร (เช่นเหรียญแบบ broker) — ข้ามเหรียญนี้ไปเลย ไปซื้อเหรียญถัดไปแทน
            state.setdefault("unbuyable", {})[name] = outcome["error"]
            return "skip"
        if "error code 18" in outcome["error"]:
            return "stop"  # เงินบาทในบัญชีจริงไม่พอ ลองใหม่รอบหน้า
        return "skip"


def _rebuy_blocked(name, cycle, dashboard_so_far):
    if name not in cycle.get("sold", {}):
        return False
    sold_info = cycle["sold"][name]
    sold_at = sold_info.get("at") if isinstance(sold_info, dict) else None
    ok, why = rebuy_check(name, dashboard_so_far, sold_at)
    if not ok:
        print(f"{name}: ขายไปแล้วรอบนี้ ยังไม่ซื้อกลับ — {why}")
        analysis_log.event("rebuy_skipped", name, reason=why)
    return not ok


def run_buys(state, cycle, today, dashboard_so_far):
    """
    ซื้อไล่ตามลำดับ (Golden Cross ก่อน แล้วความเสี่ยงน้อย -> มาก) ด้วยเงินในกอง
    รอบที่ 1: เติมแต่ละเหรียญให้ทุนครบ DCA_UNIT_THB (ถือน้อยกว่าก็ซื้อเพิ่มส่วนที่ขาด; เงินไม่พอก็ซื้อเท่าที่มี)
    รอบที่ 2: ทุกเหรียญครบแล้วยังมีเงินเหลือ -> ซื้อซ้ำเหรียญตามลำดับเดิม ไม่เกิน MAX_PER_COIN_THB ต่อเหรียญ
    ซื้อขั้นต่ำครั้งละ MIN_ORDER_THB บาท
    """
    bought_lines, failed_lines = [], []
    order = buy_order(cycle["ranking"], dashboard_so_far)
    blocked, stop = set(), False
    for cap in (DCA_UNIT_THB, MAX_PER_COIN_THB):
        for name in order:
            if stop or state["cash_pool_thb"] < MIN_ORDER_THB:
                break
            if name in state.get("unbuyable", {}) or name in blocked:
                continue
            h = state["holdings"].get(name) or {}
            holding = h.get("total_coin", 0) > 0 or any(po["name"] == name for po in state.get("pending_orders", []))
            invested = h.get("total_invested_thb", 0.0) if holding else 0.0
            if cap > DCA_UNIT_THB and not holding:
                continue  # รอบซื้อซ้ำ: เฉพาะเหรียญที่ถืออยู่แล้ว
            need = cap - invested
            if need < MIN_ORDER_THB:
                continue
            if _rebuy_blocked(name, cycle, dashboard_so_far):
                blocked.add(name)
                continue
            amt = int(min(need, state["cash_pool_thb"], DCA_UNIT_THB) * 100) / 100
            gc_priority = GOLDEN_CROSS_FIRST and is_golden_cross(name, dashboard_so_far)
            if _buy_one(state, cycle, name, amt, today, dashboard_so_far, gc_priority,
                        bought_lines, failed_lines) == "stop":
                stop = True

    state["log"] = state["log"][-20000:]  # เก็บไว้วิเคราะห์ (ตัดเฉพาะเมื่อยาวมากจริงๆ)

    if bought_lines or failed_lines:
        msg = f"💰 DCA ซื้อตามลำดับความเสี่ยง — กองเงินบอทคงเหลือ {state['cash_pool_thb']:,.2f} บาท\n"
        skipped = [n for n in state.get("unbuyable", {}) if any(f"  {n}:" in l for l in failed_lines)]
        if bought_lines:
            msg += "ซื้อแล้ว:\n" + "\n".join(bought_lines) + "\n"
        if failed_lines:
            msg += "⚠️ ล้มเหลว:\n" + "\n".join(failed_lines)
            if skipped:
                msg += f"\n(ข้าม {', '.join(skipped)} ถาวร — ซื้อผ่าน API ไม่ได้ ไปซื้อเหรียญถัดไปแทน)"
        send_telegram(msg)


# ---------------- โปรแกรมหลัก ----------------

def main():
    state = load_json(DCA_STATE_FILE, default_state())
    dashboard_so_far = load_json(DASHBOARD_FILE, {})
    control = load_json(CONTROL_FILE, {"paused": False})
    paused = bool(control.get("paused"))

    # รายชื่อเหรียญ "ติดเทรนด์" ล่าสุด (คำนวณไว้แล้วโดย crypto_alert.py ที่รันก่อนหน้าในงานเดียวกัน)
    # ถ้าไม่มีข้อมูล (เช่นรันสคริปต์นี้แยกเดี่ยว) ให้ fallback ไปใช้รายชื่อตั้งต้น
    active_coins = dashboard_so_far.get("active_coins") or list(DEFAULT_SEED_COINS)

    # กันไฟล์เก่า/เหรียญติดเทรนด์ใหม่ที่ยังไม่เคยมีใน state (เพิ่มเข้าไปแบบยอดเริ่มต้นเป็นศูนย์)
    for name in active_coins:
        state["holdings"].setdefault(
            name,
            {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0, "last_bought_date": None},
        )
    state.setdefault("sell_log", [])
    state.setdefault("realized_pnl_total_thb", 0.0)
    state.setdefault("last_summary_date", None)
    state.setdefault("crash_alert_active", False)
    state.setdefault("pending_orders", [])
    state.setdefault("cash_pool_thb", 0.0)
    state.setdefault("profit_reserved_thb", 0.0)
    if state.get("dca_cycle"):
        state["dca_cycle"].pop("budget_remaining_thb", None)
        state["dca_cycle"].pop("skipped_budget", None)

    trade_journal.save(load_journal(state))  # สร้าง/บันทึกสมุดเทรดเสมอ (ครั้งแรกจะดึงไม้เดิมจากประวัติการซื้อ)

    trading_enabled = bool(BITKUB_API_KEY and BITKUB_API_SECRET)
    today = today_str()

    # ยืนยันผลคำสั่งซื้อที่ส่งไปรอบก่อนๆ แต่ยังไม่รู้ราคา/จำนวนเหรียญจริง
    if trading_enabled and state["pending_orders"]:
        reconcile_pending_orders(state, today)

    ticker = {}
    try:
        ticker = get_ticker()
    except requests.RequestException as e:
        print(f"ดึงราคาล่าสุดไม่สำเร็จ: {e}")
    ticker_ok = isinstance(ticker, dict) and len(ticker) > 0

    # ---- ยอดเติมเงินที่ผู้ใช้แจ้งผ่าน Telegram ("เติม 300") -> เข้ากองเงินบอท ----
    apply_inbox(state)

    if not trading_enabled:
        print("ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET — ข้าม DCA รอบนี้ (ยังไม่ซื้อจริง)")
    elif paused:
        print("ระบบถูกสั่งหยุดทำงานผ่าน Telegram อยู่ — ข้ามการซื้อขาย DCA รอบนี้ทั้งหมด")
    else:
        cycle = state.get("dca_cycle")
        if cycle is None or days_since(cycle["cycle_date"], today) >= DCA_INTERVAL_DAYS:
            cycle = build_new_cycle(today, active_coins, dashboard_so_far)
            state["dca_cycle"] = cycle
            print(f"เริ่มรอบจัดลำดับใหม่ ({today}) ลำดับเหรียญ: {', '.join(cycle['ranking']) or '-'}")
        elif WHOLE_ACCOUNT_MODE:
            # โหมดเติมทุนตามเป้า: จัดลำดับใหม่ทุกชั่วโมงจากเหรียญที่ติดเทรนด์ล่าสุด (ไม่ล็อกทั้งสัปดาห์)
            # ไม่งั้นเหรียญที่เพิ่งติดเทรนด์จะไม่ได้ซื้อ แล้วเงินไหลไปซื้อซ้ำเหรียญเดิมแทน
            cycle["ranking"] = build_new_cycle(today, active_coins, dashboard_so_far)["ranking"]
        sync_wallet(state)
        run_rebalance(state, ticker, today)
        # ขายก่อน (เงินทุนที่ได้คืนจะได้ใช้ซื้อเหรียญถัดไปได้ในรอบเดียวกัน) แล้วค่อยซื้อ
        run_take_profit_stop_loss(state, ticker, today, dashboard_so_far)
        run_buys(state, cycle, today, dashboard_so_far)
        update_equity(state, ticker)

    # ----- คำนวณมูลค่าปัจจุบันของแต่ละเหรียญ (ใช้ได้ทั้งตอนเปิด/ปิดใช้งานจริง) -----
    cycle = state.get("dca_cycle") or {}
    per_coin = {}
    total_invested = 0.0
    total_value = 0.0
    for name in state["holdings"]:
        h = state["holdings"][name]
        current_price = None
        tick = ticker.get(trade_symbol_of(name)) if isinstance(ticker, dict) else None
        if isinstance(tick, dict):
            current_price = tick.get("last")
        has_pending = any(po["name"] == name for po in state.get("pending_orders", []))
        current_value = (h["total_coin"] * current_price) if current_price and not has_pending else None
        avg_cost = (h["total_invested_thb"] / h["total_coin"]) if h["total_coin"] else None
        pnl_thb = (current_value - h["total_invested_thb"]) if current_value is not None else None
        pnl_pct = (pnl_thb / h["total_invested_thb"] * 100) if pnl_thb is not None and h["total_invested_thb"] else None
        info = dashboard_so_far.get(name, {}) if isinstance(dashboard_so_far, dict) else {}

        per_coin[name] = {
            "active": name in active_coins,
            "total_invested_thb": round(h["total_invested_thb"], 2),
            "total_coin": h["total_coin"],
            "buy_count": h["buy_count"],
            "last_bought_date": h["last_bought_date"],
            "avg_cost": round(avg_cost, 4) if avg_cost is not None else None,
            "current_price": current_price,
            "current_value_thb": round(current_value, 2) if current_value is not None else None,
            "pnl_thb": round(pnl_thb, 2) if pnl_thb is not None else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "risk_pct": info.get("risk_pct"),
            "news_sentiment": info.get("news_sentiment"),
            "bought_this_cycle": bool(cycle.get("bought", {}).get(name)),
            "signal": info.get("signal"),
        }
        if has_pending:
            per_coin[name]["pending_confirmation"] = True
            continue  # ยังไม่รู้จำนวนเหรียญจริง ไม่นับรวมในพอร์ต กันตัวเลขขาดทุนหลอก
        total_invested += h["total_invested_thb"]
        if current_value is not None:
            total_value += current_value

    # หมายเหตุ: ถ้าดึงราคาล่าสุด (ticker) ไม่สำเร็จรอบนี้ ไม่คำนวณ pnl รวม (เป็น None)
    # กันไม่ให้เข้าใจผิดว่าพอร์ตขาดทุน 100% ทั้งที่จริงแค่ดึงราคาไม่ได้ชั่วคราว
    if ticker_ok:
        total_pnl = total_value - total_invested if total_invested else 0.0
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else None
    else:
        total_pnl = None
        total_pnl_pct = None

    # ----- สรุปพอร์ตรวมส่ง Telegram ทุกรอบ SUMMARY_INTERVAL_DAYS วัน -----
    if total_invested and (
        state["last_summary_date"] is None
        or days_since(state["last_summary_date"], today) >= SUMMARY_INTERVAL_DAYS
    ):
        lines = [f"📊 สรุปพอร์ต DCA รายสัปดาห์ ({today})"]
        lines.append(f"ลงทุนสะสมรวม: {total_invested:,.2f} บาท")
        lines.append(f"มูลค่าปัจจุบัน: {total_value:,.2f} บาท")
        if total_pnl is not None and total_pnl_pct is not None:
            pnl_sign = "+" if total_pnl >= 0 else ""
            lines.append(
                f"กำไร/ขาดทุนที่ยังไม่ขาย: {pnl_sign}{total_pnl:,.2f} บาท "
                f"({pnl_sign}{total_pnl_pct:.2f}%)"
            )
        realized = state.get("realized_pnl_total_thb", 0.0)
        if realized:
            lines.append(f"กำไรที่ขายทำแล้วสะสม: {'+' if realized >= 0 else ''}{realized:,.2f} บาท")
        lines.append("")
        for name, c in per_coin.items():
            if not c["buy_count"]:
                continue
            pct = c["pnl_pct"]
            pct_str = f"{'+' if pct and pct >= 0 else ''}{pct:.1f}%" if pct is not None else "-"
            lines.append(f"  {name}: ลงทุน {c['total_invested_thb']:,.0f} บาท, {pct_str}")
        send_telegram("\n".join(lines))
        state["last_summary_date"] = today

    # ----- แจ้งเตือนครั้งเดียวถ้าพอร์ตรวมขาดทุนหนักผิดปกติ (ไม่หยุดซื้อ แค่แจ้งให้รู้) -----
    if total_pnl_pct is not None:
        if total_pnl_pct <= CRASH_ALERT_THRESHOLD_PCT and not state.get("crash_alert_active"):
            send_telegram(
                f"🔻 เตือน: พอร์ต DCA รวมขาดทุน {total_pnl_pct:.1f}% จากทุนสะสม "
                f"(เกินเกณฑ์ {CRASH_ALERT_THRESHOLD_PCT}%) — DCA ยังซื้อต่อตามปกติ ไม่ได้หยุดอัตโนมัติ "
                f"แจ้งให้ทราบเฉยๆ เผื่ออยากพิจารณาเพิ่มเงินหรือปรับแผน"
            )
            state["crash_alert_active"] = True
        elif total_pnl_pct >= CRASH_RECOVERY_THRESHOLD_PCT and state.get("crash_alert_active"):
            send_telegram(f"🔺 พอร์ต DCA ฟื้นตัวกลับมาที่ {total_pnl_pct:+.1f}% แล้ว")
            state["crash_alert_active"] = False

    save_json(DCA_STATE_FILE, state)

    dashboard = load_json(DASHBOARD_FILE, {})
    dashboard["dca_bot"] = {
        "enabled": trading_enabled,
        "paused": paused,
        "active_coins": active_coins,
        "cash_pool_thb": state.get("cash_pool_thb", 0.0),
        "profit_reserved_thb": state.get("profit_reserved_thb", 0.0),
        "withdrawn_total_thb": state.get("withdrawn_total_thb", 0.0),
        "whole_account": bool(state.get("whole_account_since")),
        "capital_thb": state.get("capital_thb"),
        "equity_thb": state.get("last_equity_thb"),
        "max_per_coin_thb": MAX_PER_COIN_THB,
        "withdraw_alert_thb": PROFIT_WITHDRAW_ALERT_THB,
        "golden_cross_first": GOLDEN_CROSS_FIRST,
        "unit_thb_per_buy": DCA_UNIT_THB,
        "interval_days": DCA_INTERVAL_DAYS,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "cut_zone_pct": CUT_ZONE_PCT,
        "signal_cut_enabled": SIGNAL_CUT_ENABLED,
        "rebuy_cooldown_hours": REBUY_COOLDOWN_HOURS,
        "journal": trade_journal.summary(load_journal(state)),
        "market_sentiment": dashboard_so_far.get("market_sentiment") if isinstance(dashboard_so_far, dict) else None,
        "cycle_date": cycle.get("cycle_date"),
        "cycle_ranking": cycle.get("ranking", []),
        "realized_pnl_total_thb": state.get("realized_pnl_total_thb", 0.0),
        "coins": per_coin,
        "total_invested_thb": round(total_invested, 2),
        "total_value_thb": round(total_value, 2) if (total_invested and ticker_ok) else None,
        "total_pnl_thb": round(total_pnl, 2) if total_pnl is not None else None,
        "total_pnl_pct": round(total_pnl_pct, 2) if total_pnl_pct is not None else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(DASHBOARD_FILE, dashboard)

    # ---- บันทึกสะสมไว้วิเคราะห์ย้อนหลัง (ไม่เขียนทับ) ----
    try:
        analysis_log.record_snapshot(dashboard)
        analysis_log.record_equity(state, dashboard.get("dca_bot"))
        analysis_log.record_config({
            "unit_thb": DCA_UNIT_THB, "interval_days": DCA_INTERVAL_DAYS,
            "take_profit_pct": TAKE_PROFIT_PCT, "stop_loss_pct": STOP_LOSS_PCT,
            "signal_cut_enabled": SIGNAL_CUT_ENABLED, "cut_zone_pct": CUT_ZONE_PCT,
            "golden_cross_first": GOLDEN_CROSS_FIRST, "withdraw_alert_thb": PROFIT_WITHDRAW_ALERT_THB,
            "whole_account": WHOLE_ACCOUNT_MODE, "reinvest_profit": REINVEST_PROFIT,
            "max_per_coin_thb": MAX_PER_COIN_THB, "min_order_thb": MIN_ORDER_THB,
            "rebuy_max_risk_pct": REBUY_MAX_RISK_PCT, "rebuy_cooldown_hours": REBUY_COOLDOWN_HOURS,
            "news_sentiment_weight": NEWS_SENTIMENT_WEIGHT, "must_include": MUST_INCLUDE_COINS,
        })
        analysis_log.flush_events()
    except Exception as e:  # noqa: BLE001 — การบันทึกพังต้องไม่ทำให้บอทหยุด
        print(f"บันทึกข้อมูลวิเคราะห์ไม่สำเร็จ: {e}")

    print("dca_bot: ทำงานเสร็จสิ้น")


if __name__ == "__main__":
    main()
