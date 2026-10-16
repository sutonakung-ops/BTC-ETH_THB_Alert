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

from bitkub_trade import BitkubAPIError, place_ask, place_bid

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
# ครั้งละ DCA_UNIT_THB บาท จนกว่างบ WEEKLY_BUDGET_THB จะหมด (เหรียญที่อยู่ท้ายแถวถ้างบไม่พอ จะไม่ได้ซื้อ
# สัปดาห์นั้น — ตามที่ผู้ใช้ต้องการ "ไล่ซื้อจากเสี่ยงน้อยสุดไปมากสุด อันดับสุดท้ายไม่ต้องซื้อถ้างบหมด")
#
# คะแนนความเสี่ยง = risk_pct (ความผันผวนราคาย้อนหลัง 48 ชม. ที่ crypto_alert.py คำนวณไว้)
# หักลบด้วยคะแนนข่าว/เซนติเมนต์ (news_sentiment จาก CryptoPanic, -1 ถึง +1) คูณน้ำหนัก
# ข่าวดี -> คะแนนความเสี่ยงลดลง (มีโอกาสถูกเลือกซื้อก่อน), ข่าวร้าย -> คะแนนเพิ่มขึ้น (ถูกเลื่อนไปท้ายแถว)
WEEKLY_BUDGET_THB = 300
DCA_UNIT_THB = 50  # ซื้อเหรียญละเท่านี้ต่อครั้งที่ถูกเลือก (300/50 = ซื้อได้สูงสุด 6 เหรียญ/สัปดาห์)
DCA_INTERVAL_DAYS = 7
NEWS_SENTIMENT_WEIGHT = 5.0  # ข่าวบวก/ลบเต็มสเกล (+-1) ขยับคะแนนความเสี่ยงได้สูงสุด 5 แต้ม

# ขายทำกำไรอัตโนมัติ: เหรียญไหนกำไรเฉลี่ยรวมเกิน % นี้ ขายทั้งหมดกลับเป็นเงินบาททันที
# แล้วเริ่มสะสม DCA ใหม่จากศูนย์สำหรับเหรียญนั้น (เหรียญอื่นไม่กระทบ)
TAKE_PROFIT_PCT = 10.0

# ส่งสรุปพอร์ตรวมทาง Telegram ทุกๆ กี่วัน (รายสัปดาห์)
SUMMARY_INTERVAL_DAYS = 7

# แจ้งเตือนครั้งเดียวถ้าพอร์ตรวม (unrealized) ขาดทุนเกินระดับนี้ — ไม่ใช่เงื่อนไขหยุดซื้อ
# (DCA ยังคงซื้อต่อตามปกติ แค่แจ้งให้รู้) และจะไม่แจ้งซ้ำจนกว่าจะฟื้นกลับมาเกิน CRASH_RECOVERY_THRESHOLD_PCT ก่อน
CRASH_ALERT_THRESHOLD_PCT = -15.0
CRASH_RECOVERY_THRESHOLD_PCT = -5.0

# รายชื่อเหรียญตั้งต้น (ใช้ตอนยังไม่เคยมี state มาก่อน) — หลังจากนั้นรายชื่อเหรียญ "ที่กำลังซื้ออยู่" จะถูกปรับ
# อัตโนมัติทุกรอบตามเหรียญติดเทรนด์ล่าสุด (ดู active_coins ที่ crypto_alert.py คำนวณไว้ใน latest.json)
# เหรียญที่เคยซื้อสะสมไว้แล้วแต่หลุดเทรนด์จะ "ไม่ซื้อเพิ่ม" แต่ยังถือของเดิมรอ take-profit ตามปกติ ไม่ขายทิ้ง
DEFAULT_SEED_COINS = ["BTC", "ETH", "XRP", "ADA", "DOGE", "SOL", "KUB"]


def trade_symbol_of(name):
    return f"THB_{name}"


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
        "dca_cycle": None,  # รอบงบรวมรายสัปดาห์ปัจจุบัน (ดู build_new_cycle)
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
    ranked, unranked = [], []
    for name in active_coins:
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
        "budget_remaining_thb": WEEKLY_BUDGET_THB,
        "bought": {},
        "skipped_budget": {},
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


def execute_sell(trade_symbol, amount_coin):
    """สั่งขายจริงทั้งหมดที่ถืออยู่ของเหรียญนี้ (market order) กลับเป็นเงินบาท"""
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        return {"ok": False, "error": "ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET"}
    try:
        result = place_ask(BITKUB_API_KEY, BITKUB_API_SECRET, trade_symbol, amount_coin, rat=0, typ="market")
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

    proceeds_thb = None
    for key in ("rec",):
        val = res.get(key) if isinstance(res, dict) else None
        if val:
            proceeds_thb = float(val)
            break
    if proceeds_thb is None and sell_price:
        proceeds_thb = amount_coin * sell_price  # ประมาณการ ยังไม่หักค่าธรรมเนียม

    if not sell_price or proceeds_thb is None:
        return {"ok": False, "error": f"อ่านผลลัพธ์คำสั่งขายไม่ได้: {res}"}

    return {"ok": True, "sell_price": sell_price, "proceeds_thb": proceeds_thb}


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

    trading_enabled = bool(BITKUB_API_KEY and BITKUB_API_SECRET)
    today = today_str()

    ticker = {}
    try:
        ticker = get_ticker()
    except requests.RequestException as e:
        print(f"ดึงราคาล่าสุดไม่สำเร็จ: {e}")
    ticker_ok = isinstance(ticker, dict) and len(ticker) > 0

    if not trading_enabled:
        print("ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET — ข้าม DCA รอบนี้ (ยังไม่ซื้อจริง)")
    elif paused:
        print("ระบบถูกสั่งหยุดทำงานผ่าน Telegram อยู่ — ข้ามการซื้อ DCA รอบนี้ทั้งหมด")
    else:
        # งบรวม WEEKLY_BUDGET_THB บาท/รอบ (รอบละ DCA_INTERVAL_DAYS วัน) — เริ่มรอบใหม่เมื่อครบกำหนด
        # แล้วล็อกลำดับเหรียญที่จะซื้อ (เรียงตามความเสี่ยงน้อย->มาก) ไว้ทั้งรอบ
        cycle = state.get("dca_cycle")
        if cycle is None or days_since(cycle["cycle_date"], today) >= DCA_INTERVAL_DAYS:
            cycle = build_new_cycle(today, active_coins, dashboard_so_far)
            state["dca_cycle"] = cycle
            print(f"เริ่มรอบ DCA ใหม่ ({today}) งบ {WEEKLY_BUDGET_THB} บาท ลำดับเหรียญ: {', '.join(cycle['ranking']) or '-'}")

        # เงินที่เติมเข้าบัญชีแต่ละรอบอาจไม่สม่ำเสมอ — เหรียญที่เงินไม่พอจะ "ยังไม่ถูกบันทึกว่าซื้อแล้ว"
        # (ไม่หักงบ) จึงถูกลองซื้อใหม่ทุกชั่วโมงจนกว่าจะมีเงินพอ หรือจนกว่ารอบนี้จะหมดอายุ (ครบ 7 วัน)
        bought_lines, failed_lines = [], []
        for name in cycle["ranking"]:
            if name in cycle["bought"] or name in cycle["skipped_budget"]:
                continue
            if cycle["budget_remaining_thb"] < DCA_UNIT_THB:
                cycle["skipped_budget"][name] = True
                continue
            outcome = execute_buy(trade_symbol_of(name), DCA_UNIT_THB)
            if outcome["ok"]:
                h = state["holdings"].setdefault(
                    name, {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0, "last_bought_date": None}
                )
                h["total_invested_thb"] = round(h["total_invested_thb"] + DCA_UNIT_THB, 4)
                h["total_coin"] = h["total_coin"] + outcome["amount_coin"]
                h["buy_count"] = h["buy_count"] + 1
                h["last_bought_date"] = today
                state["log"].append({
                    "date": today,
                    "name": name,
                    "invested_thb": DCA_UNIT_THB,
                    "fill_price": outcome["fill_price"],
                    "amount_coin": outcome["amount_coin"],
                })
                cycle["bought"][name] = True
                cycle["budget_remaining_thb"] = round(cycle["budget_remaining_thb"] - DCA_UNIT_THB, 2)
                bought_lines.append(f"  {name}: {DCA_UNIT_THB} บาท @ {outcome['fill_price']:.4f}")
            else:
                failed_lines.append(f"  {name}: ล้มเหลว — {outcome['error']}")
                state["last_errors"] = state.get("last_errors", {})
                state["last_errors"][name] = {"error": outcome["error"], "at": datetime.now(timezone.utc).isoformat()}

        state["log"] = state["log"][-500:]  # กันไฟล์โตไม่จำกัด เก็บแค่ 500 รายการล่าสุด

        if bought_lines or failed_lines:
            skipped_names = [n for n in cycle["ranking"] if n in cycle["skipped_budget"]]
            msg = (
                f"💰 DCA รอบสัปดาห์นี้ ({cycle['cycle_date']}) — งบคงเหลือ {cycle['budget_remaining_thb']} "
                f"จาก {WEEKLY_BUDGET_THB} บาท\n"
            )
            if bought_lines:
                msg += "ซื้อแล้ว:\n" + "\n".join(bought_lines) + "\n"
            if failed_lines:
                msg += (
                    "⚠️ ล้มเหลว (อาจเพราะเงินในบัญชีไม่พอ — จะลองใหม่อัตโนมัติทุกชั่วโมงจนกว่าจะเติมเงินพอ):\n"
                    + "\n".join(failed_lines) + "\n"
                )
            if skipped_names:
                msg += f"⏭ งบหมดก่อนถึงคิว (รอบนี้): {', '.join(skipped_names)}"
            send_telegram(msg)
        elif not cycle["ranking"]:
            print("ยังไม่มีเหรียญติดเทรนด์ให้เลือกซื้อในรอบนี้")
        else:
            print("รอบ DCA สัปดาห์นี้ซื้อ/ข้ามครบทุกเหรียญในคิวแล้ว รอรอบถัดไป")

    # ----- เช็ค take-profit: เหรียญไหนกำไรเฉลี่ยรวมเกิน TAKE_PROFIT_PCT ขายทั้งหมดกลับเป็นเงินบาท -----
    # เช็คทุกเหรียญที่เคยถือสะสมไว้ (ไม่ใช่แค่เหรียญที่ยังติดเทรนด์) เพราะของเก่าที่หลุดเทรนด์ก็ยังต้องขายได้ตามแผน
    if trading_enabled and not paused:
        for name in list(state["holdings"].keys()):
            h = state["holdings"][name]
            if h["total_coin"] <= 0:
                continue
            tick = ticker.get(trade_symbol_of(name)) if isinstance(ticker, dict) else None
            current_price = tick.get("last") if isinstance(tick, dict) else None
            if not current_price:
                continue
            avg_cost = h["total_invested_thb"] / h["total_coin"] if h["total_coin"] else None
            if not avg_cost:
                continue
            pnl_pct_now = (current_price - avg_cost) / avg_cost * 100
            if pnl_pct_now < TAKE_PROFIT_PCT:
                continue

            outcome = execute_sell(trade_symbol_of(name), h["total_coin"])
            if outcome["ok"]:
                realized_pnl = outcome["proceeds_thb"] - h["total_invested_thb"]
                state["realized_pnl_total_thb"] = round(state.get("realized_pnl_total_thb", 0.0) + realized_pnl, 2)
                state["sell_log"].append({
                    "date": today,
                    "name": name,
                    "sold_coin": h["total_coin"],
                    "sell_price": outcome["sell_price"],
                    "invested_thb": h["total_invested_thb"],
                    "proceeds_thb": round(outcome["proceeds_thb"], 2),
                    "realized_pnl_thb": round(realized_pnl, 2),
                })
                state["sell_log"] = state["sell_log"][-200:]
                send_telegram(
                    f"🎉 ขายทำกำไร {name} ทั้งหมด (กำไรเฉลี่ยรวมถึง +{pnl_pct_now:.1f}%)\n"
                    f"ทุนสะสม {h['total_invested_thb']:.2f} บาท → ได้คืน {outcome['proceeds_thb']:.2f} บาท "
                    f"(กำไร {realized_pnl:+.2f} บาท)\n"
                    f"เริ่มสะสม DCA ของ {name} ใหม่จากศูนย์ตั้งแต่รอบถัดไป"
                )
                # เริ่มสะสมใหม่จากศูนย์สำหรับเหรียญนี้ (เหรียญอื่นไม่กระทบ)
                h["total_invested_thb"] = 0.0
                h["total_coin"] = 0.0
                h["buy_count"] = 0
                h["last_bought_date"] = None
            else:
                print(f"{name}: ถึงเกณฑ์ take-profit (+{pnl_pct_now:.1f}%) แต่ขายไม่สำเร็จ — {outcome['error']} (ลองใหม่รอบหน้า)")

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
        current_value = (h["total_coin"] * current_price) if current_price else None
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
            "skipped_budget_this_cycle": bool(cycle.get("skipped_budget", {}).get(name)),
        }
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
        "weekly_budget_thb": WEEKLY_BUDGET_THB,
        "unit_thb_per_buy": DCA_UNIT_THB,
        "interval_days": DCA_INTERVAL_DAYS,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "cycle_date": cycle.get("cycle_date"),
        "cycle_ranking": cycle.get("ranking", []),
        "cycle_budget_remaining_thb": cycle.get("budget_remaining_thb"),
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
