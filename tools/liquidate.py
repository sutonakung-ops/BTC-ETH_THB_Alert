# -*- coding: utf-8 -*-
"""
ขายทุกเหรียญในบัญชีเป็นเงินบาท แล้วหยุดบอท (ผู้ใช้สั่ง 28 พ.ย. 2569: ต้องใช้เงินด่วน จะกลับมาทำระบบใหม่ตอนพร้อม)
1) หยุดบอทก่อน (paused) กันรอบอื่นซื้อกลับ
2) ขายทุกเหรียญที่มูลค่า >= MIN_SELL_THB ด้วย market order (เศษที่ต่ำกว่าขั้นต่ำ Bitkub ขายไม่ได้)
3) อัปเดตบันทึกของบอท + แจ้งสรุปทาง Telegram (บอทไม่ถอนเงิน — ผู้ใช้ถอนเองในแอป Bitkub)
"""
import json, os, sys
from datetime import datetime, timezone
import requests

sys.path.insert(0, os.getcwd())
import dca_bot  # noqa: E402
import trade_journal  # noqa: E402
from bitkub_trade import get_wallet  # noqa: E402

MIN_SELL_THB = 15.0
now = datetime.now(timezone.utc).isoformat()
today = dca_bot.today_str()

# 1) หยุดบอท
ctl = dca_bot.load_json(dca_bot.CONTROL_FILE, {}) or {}
ctl.update({"paused": True, "updated_at": now, "reason": "liquidate: ผู้ใช้สั่งขายทั้งหมดเป็นเงินบาท"})
dca_bot.save_json(dca_bot.CONTROL_FILE, ctl)

ticker = requests.get("https://api.bitkub.com/api/market/ticker", timeout=20).json()
wallet = get_wallet(dca_bot.BITKUB_API_KEY, dca_bot.BITKUB_API_SECRET)
if not wallet:
    dca_bot.send_telegram("⚠️ ขายทั้งหมดไม่สำเร็จ: อ่านยอดในบัญชีไม่ได้ (บอทหยุดทำงานแล้ว ยังไม่ได้ขายอะไร)")
    sys.exit(1)
thb_before = wallet["THB"]["available"]
state = dca_bot.load_json(dca_bot.DCA_STATE_FILE, {}) or {}
holdings = state.get("holdings") or {}
dips = state.get("dip_positions") or {}

sold, failed, skipped = [], [], []
for cur, bal in sorted(wallet.items()):
    if cur == "THB":
        continue
    amt = bal.get("available") or 0.0
    px = float((ticker.get(f"THB_{cur}") or {}).get("last") or 0)
    if amt <= 0:
        continue
    if amt * px < MIN_SELL_THB:
        skipped.append((cur, amt * px))
        continue
    out = dca_bot.execute_sell(f"THB_{cur}", amt, fallback_price=px)
    if not out.get("ok"):
        failed.append((cur, out.get("error")))
        continue
    invested = ((holdings.get(cur) or {}).get("total_invested_thb") or 0.0) + ((dips.get(cur) or {}).get("invested_thb") or 0.0)
    pnl = out["proceeds_thb"] - invested if invested else None
    sold.append((cur, out["proceeds_thb"], invested, pnl, out.get("estimated")))
    state.setdefault("sell_log", []).append({
        "date": today, "name": cur, "sold_coin": out.get("sold_coin", amt), "sell_price": out["sell_price"],
        "invested_thb": round(invested, 2), "proceeds_thb": round(out["proceeds_thb"], 2),
        "realized_pnl_thb": round(pnl, 2) if pnl is not None else 0.0, "reason": "liquidate"})
    if pnl is not None:
        state["realized_pnl_total_thb"] = round(state.get("realized_pnl_total_thb", 0.0) + pnl, 2)
    if cur in holdings:
        holdings[cur].update({"total_coin": 0.0, "total_invested_thb": 0.0})
    dips.pop(cur, None)

# บันทึก journal: ปิดทุกไม้ที่เปิดอยู่ของเหรียญที่ขายแล้ว
journal = trade_journal.load()
names = {s[0] for s in sold}
for lot in journal.get("lots", []):
    if lot.get("status") == "open" and lot.get("coin") in names:
        lot.update({"status": "closed", "closed_at": now, "exit_reason": "liquidate"})
trade_journal.save(journal)

after = get_wallet(dca_bot.BITKUB_API_KEY, dca_bot.BITKUB_API_SECRET) or {}
thb_after = (after.get("THB") or {}).get("available", thb_before + sum(s[1] for s in sold))
state["holdings"], state["dip_positions"] = holdings, dips
state["cash_pool_thb"] = round(thb_after, 2)
state["pending_orders"] = []
state["liquidated_at"] = now
dca_bot.save_json(dca_bot.DCA_STATE_FILE, state)

lines = ["💵 ขายทุกเหรียญเป็นเงินบาทแล้ว (ผู้ใช้สั่ง) — บอทหยุดทำงาน"]
for cur, thb, inv, pnl, est in sold:
    lines.append(f"  {cur}: ได้ {thb:,.2f} บาท" + (f" (ทุน {inv:,.2f} · {pnl:+,.2f})" if pnl is not None else "") + (" ~ประมาณ" if est else ""))
total_pnl = sum(s[3] for s in sold if s[3] is not None)
lines.append(f"รวมขายได้ {sum(s[1] for s in sold):,.2f} บาท · กำไร/ขาดทุนรอบนี้ {total_pnl:+,.2f} บาท")
cap = state.get("capital_thb")
if cap:
    lines.append(f"ทุนทั้งบัญชี {cap:,.2f} → เงินบาทตอนนี้ {thb_after:,.2f} บาท ({thb_after - cap:+,.2f})")
lines.append(f"💰 เงินบาทพร้อมถอน: {thb_after:,.2f} บาท — ถอนเข้าบัญชีธนาคารได้ในแอป Bitkub")
if skipped:
    lines.append("เศษที่ต่ำกว่าขั้นต่ำ ขายไม่ได้: " + ", ".join(f"{c} ~{v:.2f} บาท" for c, v in skipped))
if failed:
    lines.append("⚠️ ขายไม่สำเร็จ: " + ", ".join(f"{c} ({e})" for c, e in failed))
lines.append("กลับมาใช้บอทเมื่อพร้อม: บอก Claude ให้ตั้งระบบใหม่ (พิมพ์ \"เริ่ม\" จะทำให้บอทกลับมาซื้อทันที)")
dca_bot.send_telegram("\n".join(lines))
print("sold", len(sold), "failed", len(failed), "skipped", len(skipped))
sys.exit(1 if failed else 0)
