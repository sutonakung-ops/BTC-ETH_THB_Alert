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

import trade_journal
from bitkub_trade import BitkubAPIError, fetch_fill, place_ask, place_bid

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
DCA_UNIT_THB = 50  # ซื้อเหรียญละเท่านี้ต่อครั้ง
DCA_INTERVAL_DAYS = 7
MUST_INCLUDE_COINS = ["BTC", "ETH"]
# เหรียญที่ขาย (TP/SL) ไปแล้วในรอบนี้ ซื้อกลับได้ในรอบเดียวกันถ้าประเมินความเสี่ยงตอนนั้นแล้วผ่าน:
# ความผันผวน 48 ชม. (risk_pct) ไม่เกินค่านี้ และแนวโน้ม MA ไม่ใช่ขาลง (Death Cross)
REBUY_MAX_RISK_PCT = 3.0  # อยู่ในตัวเลือกเสมอ แม้ไม่ติดอันดับปริมาณซื้อขาย
DEPOSIT_INBOX_FILE = "state/deposit_inbox.json"  # crypto_alert.py เขียนยอดเติมเงินจาก Telegram ไว้ที่นี่
NEWS_SENTIMENT_WEIGHT = 5.0  # ข่าวบวก/ลบเต็มสเกล (+-1) ขยับคะแนนความเสี่ยงได้สูงสุด 5 แต้ม

# ขายอัตโนมัติ (ผู้ใช้เลือกให้ใช้ TP/SL ชุดเดียวกับบอทเทรดกับเหรียญ DCA แทนการเปิดไม้ใหม่ด้วยเงินบาท)
# เทียบราคาปัจจุบันกับทุนเฉลี่ยของแต่ละเหรียญ: กำไรถึง TAKE_PROFIT_PCT หรือขาดทุนถึง STOP_LOSS_PCT
# ขายเหรียญนั้นทั้งหมดกลับเป็นเงินบาท แล้วเริ่มสะสม DCA ใหม่จากศูนย์ (เหรียญอื่นไม่กระทบ)
TAKE_PROFIT_PCT = 4.0
STOP_LOSS_PCT = 5.0
# ตัดขาดทุนตามสัญญาณ (ผู้ใช้เลือกทางเลือกที่ 4): ขาดทุนตั้งแต่ CUT_ZONE_PCT ขึ้นไป และแนวโน้ม MA เป็นขาลง
# (Death Cross) -> ขายเลยไม่ต้องรอถึง STOP_LOSS_PCT / ถ้าแนวโน้มยังไม่ลง ถือรอ (-5% ยังเป็นเพดานเสมอ)
CUT_ZONE_PCT = 3.0

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
    ถ้าเงินไม่พอเพราะจำนวนเหรียญจริงน้อยกว่าที่บันทึกไว้เล็กน้อย (ค่าธรรมเนียม/ปัดเศษ) ลองใหม่ที่ 99.5%
    """
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        return {"ok": False, "error": "ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET"}
    sym = order_symbol_of(trade_symbol)
    result, res = None, None
    for factor in (1.0, 0.995):
        amt = float(f"{amount_coin * factor:.8f}")
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
        return {"ok": True, "order_id": order_id, "sell_price": fill["rate"], "proceeds_thb": fill["thb"]}
    if fallback_price:
        # ขายไปแล้วจริงแต่ยังอ่านผลไม่ได้ — ประมาณจากราคาตลาดล่าสุด (ไม่หักค่าธรรมเนียม)
        return {"ok": True, "order_id": order_id, "sell_price": fallback_price,
                "proceeds_thb": amount_coin * fallback_price, "estimated": True}
    return {"ok": True, "order_id": order_id, "sell_price": 0.0, "proceeds_thb": 0.0, "estimated": True}


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
        elif pnl_pct_now <= -CUT_ZONE_PCT and "Death Cross" in signal:
            reason = "signal_cut"
        else:
            if pnl_pct_now <= -CUT_ZONE_PCT:
                print(f"{name}: ขาดทุน {pnl_pct_now:.1f}% แต่แนวโน้มยังไม่ใช่ขาลง ({signal or 'ไม่มีข้อมูล'}) — ถือรอ")
            continue
        is_stop_loss = reason != "take_profit"

        outcome = execute_sell(trade_symbol_of(name), h["total_coin"], fallback_price=current_price)
        if outcome["ok"]:
            invested = h["total_invested_thb"]
            realized_pnl = outcome["proceeds_thb"] - invested
            if realized_pnl > 0:
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
            state["sell_log"] = state["sell_log"][-200:]
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
                   if realized_pnl > 0 else "")
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
    if risk_pct > REBUY_MAX_RISK_PCT:
        return False, f"ผันผวน {risk_pct:.2f}% เกิน {REBUY_MAX_RISK_PCT}%"
    if "Death Cross" in str(info.get("signal", "")):
        return False, "แนวโน้มขาลง (Death Cross)"
    return True, f"ผันผวน {risk_pct:.2f}% แนวโน้มไม่ใช่ขาลง"


def run_buys(state, cycle, today, dashboard_so_far):
    """ซื้อไล่ตามลำดับความเสี่ยงด้วยเงินในกอง ครั้งละ DCA_UNIT_THB จนเงินในกองไม่พอ"""
    bought_lines, failed_lines = [], []
    for name in cycle["ranking"]:
        if name in cycle["bought"]:
            continue
        if state["cash_pool_thb"] < DCA_UNIT_THB:
            break
        if name in cycle.get("sold", {}):
            sold_info = cycle["sold"][name]
            sold_at = sold_info.get("at") if isinstance(sold_info, dict) else None
            ok, why = rebuy_check(name, dashboard_so_far, sold_at)
            if not ok:
                print(f"{name}: ขายไปแล้วรอบนี้ ยังไม่ซื้อกลับ — {why}")
                continue
        outcome = execute_buy(trade_symbol_of(name), DCA_UNIT_THB)
        if outcome["ok"]:
            journal = load_journal(state)
            trade_journal.record_buy(
                journal, name, outcome["order_id"], DCA_UNIT_THB,
                outcome.get("fill_price"), outcome.get("amount_coin"), dashboard_so_far,
                rebuy=name in cycle.get("sold", {}),
            )
            trade_journal.save(journal)
            h = state["holdings"].setdefault(
                name, {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0, "last_bought_date": None}
            )
            h["total_invested_thb"] = round(h["total_invested_thb"] + DCA_UNIT_THB, 4)
            h["buy_count"] = h["buy_count"] + 1
            h["last_bought_date"] = today
            if outcome.get("pending"):
                # Bitkub รับคำสั่งแล้ว แต่ยังอ่านราคาจริงไม่ได้ — เก็บไว้ยืนยันรอบหน้า (ไม่ซื้อซ้ำ)
                state["pending_orders"].append({
                    "name": name, "sym": outcome["sym"], "order_id": outcome["order_id"],
                    "side": "buy", "amt_thb": DCA_UNIT_THB, "date": today,
                })
                bought_lines.append(f"  {name}: {DCA_UNIT_THB} บาท (รอยืนยันราคา)")
            else:
                h["total_coin"] = h["total_coin"] + outcome["amount_coin"]
                state["log"].append({
                    "date": today,
                    "name": name,
                    "invested_thb": DCA_UNIT_THB,
                    "fill_price": outcome["fill_price"],
                    "amount_coin": outcome["amount_coin"],
                    "order_id": outcome["order_id"],
                })
                bought_lines.append(f"  {name}: {DCA_UNIT_THB} บาท @ {outcome['fill_price']:,.4f}")
            if cycle.get("sold", {}).pop(name, None):
                bought_lines[-1] += " (ซื้อกลับ — ผ่านการประเมินความเสี่ยง)"
            cycle["bought"][name] = True
            state["cash_pool_thb"] = round(state["cash_pool_thb"] - DCA_UNIT_THB, 2)
            state.get("last_errors", {}).pop(name, None)
        else:
            failed_lines.append(f"  {name}: ล้มเหลว — {outcome['error']}")
            state["last_errors"] = state.get("last_errors", {})
            state["last_errors"][name] = {"error": outcome["error"], "at": datetime.now(timezone.utc).isoformat()}
            if "error code 18" in outcome["error"]:
                break  # เงินบาทในบัญชีจริงไม่พอ ลองใหม่รอบหน้า

    state["log"] = state["log"][-500:]  # กันไฟล์โตไม่จำกัด เก็บแค่ 500 รายการล่าสุด

    if bought_lines or failed_lines:
        msg = f"💰 DCA ซื้อตามลำดับความเสี่ยง — กองเงินบอทคงเหลือ {state['cash_pool_thb']:,.2f} บาท\n"
        if bought_lines:
            msg += "ซื้อแล้ว:\n" + "\n".join(bought_lines) + "\n"
        if failed_lines:
            msg += "⚠️ ล้มเหลว (จะลองใหม่อัตโนมัติรอบหน้า):\n" + "\n".join(failed_lines)
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
    inbox = load_json(DEPOSIT_INBOX_FILE, [])
    if inbox:
        added = sum(float(d.get("amount_thb") or 0) for d in inbox)
        state["cash_pool_thb"] = round(state["cash_pool_thb"] + added, 2)
        save_json(DEPOSIT_INBOX_FILE, [])
        print(f"เติมเงินเข้ากองบอท {added:,.2f} บาท -> คงเหลือ {state['cash_pool_thb']:,.2f} บาท")

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
        # ขายก่อน (เงินทุนที่ได้คืนจะได้ใช้ซื้อเหรียญถัดไปได้ในรอบเดียวกัน) แล้วค่อยซื้อ
        run_take_profit_stop_loss(state, ticker, today, dashboard_so_far)
        run_buys(state, cycle, today, dashboard_so_far)

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
        "unit_thb_per_buy": DCA_UNIT_THB,
        "interval_days": DCA_INTERVAL_DAYS,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "cut_zone_pct": CUT_ZONE_PCT,
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

    print("dca_bot: ทำงานเสร็จสิ้น")


if __name__ == "__main__":
    main()
