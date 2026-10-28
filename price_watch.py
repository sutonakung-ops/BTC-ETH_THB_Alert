# -*- coding: utf-8 -*-
"""
เฝ้าราคาทุก 1 นาที (รันต่อท้ายรอบรายชั่วโมงใน GitHub Actions จนเกือบถึงรอบถัดไป)

ทุกนาที:
- อ่านคำสั่ง Telegram (หยุด/เริ่ม/เติมเงิน) ให้มีผลภายใน ~1 นาที
- เช็คราคาเหรียญที่ DCA ถืออยู่ กับกฎขาย: +5% ขายทำกำไร, -15% ตัดขาดทุน (เพดาน),
  (ตัดตามสัญญาณขาลงปิดอยู่ ดู SIGNAL_CUT_ENABLED ใน dca_bot.py)
  (สัญญาณแนวโน้มมาจาก latest.json ซึ่งวิเคราะห์จากกราฟรายชั่วโมง จึงอัปเดตชั่วโมงละครั้ง)
- ถ้ามีการขาย หรือมีเงินเข้ากอง -> ซื้อตามลำดับความเสี่ยงต่อทันที (กติกาเดียวกับ dca_bot.py)
- ถ้ามีอะไรเปลี่ยน บันทึก state แล้ว commit + push ทันที (กันข้อมูลหายถ้างานถูกตัดกลางทาง)

กฎการซื้อขายทั้งหมดใช้ฟังก์ชันเดียวกับ dca_bot.py ไม่มีกติกาแยก
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

import crypto_alert
import analysis_log
import dca_bot
import report_tg
import report_xlsx
import trade_journal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import vault  # noqa: E402

WATCH_MINUTES = float(os.environ.get("WATCH_MINUTES", "50"))
INTERVAL_SECONDS = 60


def git_commit_push(message):
    # เข้ารหัสไฟล์ข้อมูลทั้งหมดก่อน แล้ว stage เฉพาะไฟล์ .enc (ดู tools/vault.py)
    vault.lock(quiet=True)
    vault.stage_all()
    if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", f"{message} [skip ci]"], check=False)
    pushed = False
    for _ in range(3):
        subprocess.run(["git", "pull", "-q", "--rebase"], check=False)
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            pushed = True
            break
        time.sleep(5)
    if not pushed:
        # ถ้า pull ชนกัน อย่าค้าง repo ไว้กลางทาง และอย่าถอดรหัสทับไฟล์ในเครื่อง (จะได้ไม่เสียข้อมูลรอบนี้)
        subprocess.run(["git", "rebase", "--abort"], capture_output=True)
        print("push state ไม่สำเร็จ (จะลองใหม่รอบถัดไป)")
        return
    # ไฟล์ที่ workflow อื่นเพิ่งอัปเดต (เช่น ค่าที่ระบบปรับกลยุทธ์) มาเป็น .enc -> ถอดรหัสให้บอทเห็นค่าล่าสุด
    vault.unlock(quiet=True)


def snapshot(state):
    return json.dumps(
        {k: state.get(k) for k in ("holdings", "cash_pool_thb", "profit_reserved_thb", "pending_orders", "dca_cycle",
                                    "withdrawn_total_thb", "withdraw_alert_sent", "capital_thb",
                                    "dip_positions")},
        sort_keys=True, ensure_ascii=False,
    )


def check_once():
    """ทำงาน 1 รอบ คืน True ถ้ามีการเปลี่ยนแปลงที่ต้องบันทึก"""
    control_before = json.dumps(dca_bot.load_json(dca_bot.CONTROL_FILE, {}), sort_keys=True)
    try:
        control = crypto_alert.process_telegram_commands()
    except Exception as e:  # noqa: BLE001 — อ่าน Telegram ไม่ได้ ไม่ควรทำให้การเฝ้าราคาหยุด
        print(f"อ่านคำสั่ง Telegram ไม่สำเร็จ: {e}")
        control = dca_bot.load_json(dca_bot.CONTROL_FILE, {"paused": False})
    control_changed = json.dumps(dca_bot.load_json(dca_bot.CONTROL_FILE, {}), sort_keys=True) != control_before

    ticker = {}
    state = dca_bot.load_json(dca_bot.DCA_STATE_FILE, None)
    if not state:
        return control_changed
    before = snapshot(state)

    inbox = dca_bot.apply_inbox(state)

    trading_enabled = bool(dca_bot.BITKUB_API_KEY and dca_bot.BITKUB_API_SECRET)
    if trading_enabled and not control.get("paused"):
        state.setdefault("pending_orders", [])
        today = dca_bot.today_str()
        if state["pending_orders"]:
            dca_bot.reconcile_pending_orders(state, today)
        dca_bot.sync_wallet(state)
        try:
            ticker = dca_bot.get_ticker()
        except (requests.RequestException, ValueError) as e:
            print(f"ดึงราคาไม่สำเร็จ: {e}")
            ticker = {}
        dashboard = dca_bot.load_json(dca_bot.DASHBOARD_FILE, {})
        if isinstance(ticker, dict) and ticker:
            dca_bot.run_rebalance(state, ticker, today)
            dca_bot.run_take_profit_stop_loss(state, ticker, today, dashboard)
            dca_bot.run_dip(state, ticker, today, dashboard)
            cycle = state.get("dca_cycle")
            if cycle and state.get("cash_pool_thb", 0) >= dca_bot.MIN_ORDER_THB:
                dca_bot.run_buys(state, cycle, today, dashboard)
            dca_bot.update_equity(state, ticker)

    try:
        report_tg.handle_request(ticker or None)
    except Exception as e:  # noqa: BLE001 — รายงานพังต้องไม่ทำให้เฝ้าราคาหยุด
        print(f"ส่งรายงานไม่สำเร็จ: {e}")

    logged = analysis_log.flush_events(kinds={"buy", "sell", "buy_failed", "withdraw", "whole_account_start"})
    changed = snapshot(state) != before or bool(inbox) or logged
    if changed:
        dca_bot.save_json(dca_bot.DCA_STATE_FILE, state)
    return changed or control_changed


def main():
    deadline = time.time() + WATCH_MINUTES * 60
    print(f"เริ่มเฝ้าราคาทุก {INTERVAL_SECONDS} วินาที เป็นเวลา {WATCH_MINUTES:.0f} นาที")
    rounds = 0
    while time.time() < deadline:
        started = time.time()
        rounds += 1
        try:
            if check_once():
                report_xlsx.safe_build(dca_bot)
                git_commit_push(f"Price watch update {datetime.now(timezone.utc).strftime('%H:%M')}")
        except Exception as e:  # noqa: BLE001 — รอบใดรอบหนึ่งพัง อย่าให้หยุดเฝ้าทั้งชั่วโมง
            print(f"รอบที่ {rounds} ผิดพลาด: {e}")
        time.sleep(max(0, INTERVAL_SECONDS - (time.time() - started)))
    print(f"price_watch: เฝ้าครบ {rounds} รอบ จบการทำงาน")


if __name__ == "__main__":
    main()
