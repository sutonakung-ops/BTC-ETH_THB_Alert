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
DASHBOARD_FILE = "latest.json"

# ---------------- ตั้งค่า DCA (แก้ตัวเลขตรงนี้ได้เลยถ้าต้องการเปลี่ยน) ----------------
# งบ 50 บาท/เหรียญ/รอบ x 7 เหรียญ = 350 บาท/รอบ, รอบละ 7 วัน (รายสัปดาห์)
# ผู้ใช้แจ้งว่าจะ "ทยอยเติมเงิน" เข้าบัญชี Bitkub เป็นรายสัปดาห์ และยอดเติมแต่ละรอบไม่สม่ำเสมอ
# (ดูหมายเหตุเรื่อง partial-fill ด้านบน) — ไม่ใช่ทุนก้อนเดียว 500-1,000 บาทที่ตั้งไว้ตอนทดสอบ
# ระบบจับจังหวะช่วงแรก
DCA_AMOUNT_THB_PER_COIN = 50
DCA_INTERVAL_DAYS = 7

# ขายทำกำไรอัตโนมัติ: เหรียญไหนกำไรเฉลี่ยรวมเกิน % นี้ ขายทั้งหมดกลับเป็นเงินบาททันที
# แล้วเริ่มสะสม DCA ใหม่จากศูนย์สำหรับเหรียญนั้น (เหรียญอื่นไม่กระทบ)
TAKE_PROFIT_PCT = 10.0

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


def days_since(date_str, today):
    d1 = datetime.strptime(date_str, "%Y-%m-%d")
    d2 = datetime.strptime(today, "%Y-%m-%d")
    return (d2 - d1).days


def default_state():
    return {
        "holdings": {
            coin["name"]: {
                "total_invested_thb": 0.0,
                "total_coin": 0.0,
                "buy_count": 0,
                "last_bought_date": None,
            }
            for coin in DCA_COINS
        },
        "log": [],
        "sell_log": [],
        "realized_pnl_total_thb": 0.0,
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
    # กันไฟล์เก่าที่ยังไม่มีเหรียญใหม่ (เผื่อขยายรายชื่อเหรียญทีหลัง)
    for coin in DCA_COINS:
        state["holdings"].setdefault(
            coin["name"],
            {"total_invested_thb": 0.0, "total_coin": 0.0, "buy_count": 0, "last_bought_date": None},
        )
    state.setdefault("sell_log", [])
    state.setdefault("realized_pnl_total_thb", 0.0)

    trading_enabled = bool(BITKUB_API_KEY and BITKUB_API_SECRET)
    today = today_str()

    ticker = {}
    try:
        ticker = get_ticker()
    except requests.RequestException as e:
        print(f"ดึงราคาล่าสุดไม่สำเร็จ: {e}")

    if not trading_enabled:
        print("ยังไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET — ข้าม DCA รอบนี้ (ยังไม่ซื้อจริง)")
    else:
        # เช็คทีละเหรียญว่าครบรอบ DCA_INTERVAL_DAYS วันจากครั้งล่าสุดหรือยัง (แยกกันต่อเหรียญ ไม่ผูกเป็นก้อนเดียว)
        # เพราะยอดเงินที่เติมเข้าบัญชีแต่ละรอบไม่สม่ำเสมอ — เหรียญที่เงินไม่พอจะ "ยังไม่ขยับ last_bought_date"
        # จึงถูกลองซื้อใหม่ทุกชั่วโมงจนกว่าจะมีเงินพอ โดยไม่ไปซื้อซ้ำเหรียญที่ได้ไปแล้วในรอบนี้
        due_coins = []
        for coin in DCA_COINS:
            last = state["holdings"][coin["name"]]["last_bought_date"]
            if last is None or days_since(last, today) >= DCA_INTERVAL_DAYS:
                due_coins.append(coin)

        if not due_coins:
            print("ยังไม่ครบรอบ DCA ของทุกเหรียญ รอรอบถัดไป")
        else:
            bought_lines = []
            failed_lines = []
            for coin in due_coins:
                name = coin["name"]
                outcome = execute_buy(coin["trade_symbol"], DCA_AMOUNT_THB_PER_COIN)
                if outcome["ok"]:
                    h = state["holdings"][name]
                    h["total_invested_thb"] = round(h["total_invested_thb"] + DCA_AMOUNT_THB_PER_COIN, 4)
                    h["total_coin"] = h["total_coin"] + outcome["amount_coin"]
                    h["buy_count"] = h["buy_count"] + 1
                    h["last_bought_date"] = today
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

            msg = f"💰 DCA รอบสัปดาห์นี้ ({today}) — ซื้อสำเร็จ {len(bought_lines)}/{len(due_coins)} เหรียญที่ครบรอบ\n"
            if bought_lines:
                msg += "\n".join(bought_lines)
            if failed_lines:
                msg += (
                    "\n⚠️ ล้มเหลว (อาจเพราะเงินในบัญชีไม่พอ — จะลองใหม่อัตโนมัติทุกชั่วโมงจนกว่าจะเติมเงินพอ):\n"
                    + "\n".join(failed_lines)
                )
            send_telegram(msg)

    # ----- เช็ค take-profit: เหรียญไหนกำไรเฉลี่ยรวมเกิน TAKE_PROFIT_PCT ขายทั้งหมดกลับเป็นเงินบาท -----
    if trading_enabled:
        for coin in DCA_COINS:
            name = coin["name"]
            h = state["holdings"][name]
            if h["total_coin"] <= 0:
                continue
            tick = ticker.get(coin["trade_symbol"]) if isinstance(ticker, dict) else None
            current_price = tick.get("last") if isinstance(tick, dict) else None
            if not current_price:
                continue
            avg_cost = h["total_invested_thb"] / h["total_coin"] if h["total_coin"] else None
            if not avg_cost:
                continue
            pnl_pct_now = (current_price - avg_cost) / avg_cost * 100
            if pnl_pct_now < TAKE_PROFIT_PCT:
                continue

            outcome = execute_sell(coin["trade_symbol"], h["total_coin"])
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
            "last_bought_date": h["last_bought_date"],
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
        "amount_thb_per_coin_per_period": DCA_AMOUNT_THB_PER_COIN,
        "interval_days": DCA_INTERVAL_DAYS,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "realized_pnl_total_thb": state.get("realized_pnl_total_thb", 0.0),
        "coins": per_coin,
        "total_invested_thb": round(total_invested, 2),
        "total_value_thb": round(total_value, 2) if total_invested else None,
        "total_pnl_thb": round(total_pnl, 2) if total_invested else None,
        "total_pnl_pct": round(total_pnl_pct, 2) if total_pnl_pct is not None else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json(DASHBOARD_FILE, dashboard)

    print("dca_bot: ทำงานเสร็จสิ้น")


if __name__ == "__main__":
    main()
