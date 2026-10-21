# -*- coding: utf-8 -*-
"""
บันทึกข้อมูลสะสมไว้วิเคราะห์ย้อนหลัง (append-only — ไม่เขียนทับของเดิม)
เรียกจาก dca_bot.py ทุกรอบรายชั่วโมง

analysis/snapshots/YYYY-MM-DD.jsonl — ภาพตลาดรายชั่วโมง: ทุกเหรียญที่วิเคราะห์ (ราคา, เปลี่ยนแปลง 24 ชม.,
                                       ความเสี่ยง, แนวโน้ม MA, premium, ความหนากระดาน, แรงไล่ซื้อ)
analysis/equity.jsonl              — มูลค่าพอร์ตรายชั่วโมง (เหรียญ + กองเงิน + กำไรแยกเก็บ) และรายเหรียญ
analysis/config_history.jsonl      — กติกาของบอท บันทึกเฉพาะตอนที่มีการเปลี่ยนค่า
analysis/events/YYYY-MM-DD.jsonl   — เหตุผลการตัดสินใจของบอท (ซื้อ/ขาย/ไม่ซื้อคืนเพราะอะไร/ถือรอ ฯลฯ)
                                       ของรอบรายชั่วโมง (การซื้อขายระหว่างชั่วโมงบันทึกใน trade_journal อยู่แล้ว)
"""

import json
import os
from datetime import datetime, timezone

BASE = "analysis"
COIN_KEYS = ("price", "change_24h", "quote_volume_thb", "risk_pct", "signal", "gap_percent", "ma12", "ma24",
             "premium_pct", "depth_imbalance", "buy_flow_pct")
_EVENTS = []


def _now():
    return datetime.now(timezone.utc)


def _append(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def event(kind, coin=None, **info):
    """เก็บเหตุผลการตัดสินใจไว้ในหน่วยความจำ แล้วเขียนลงไฟล์ตอน flush_events()"""
    _EVENTS.append({"at": _now().isoformat(), "kind": kind, "coin": coin, **info})


def flush_events(kinds=None):
    """เขียน event ลงไฟล์ (kinds = เลือกเฉพาะบางชนิด เช่น ตัวเฝ้าราคารายนาทีเก็บแค่ซื้อ/ขาย กันไฟล์บวม)"""
    if not _EVENTS:
        return False
    keep = [e for e in _EVENTS if kinds is None or e["kind"] in kinds]
    path = f"{BASE}/events/{_now().strftime('%Y-%m-%d')}.jsonl"
    for e in keep:
        _append(path, e)
    _EVENTS.clear()
    return bool(keep)


def record_snapshot(dashboard):
    if not isinstance(dashboard, dict):
        return
    active = set(dashboard.get("active_coins") or [])
    coins = {}
    for name, d in dashboard.items():
        if isinstance(d, dict) and "price" in d and "signal" in d:
            coins[name] = {k: d.get(k) for k in COIN_KEYS}
            coins[name]["active"] = name in active
    _append(f"{BASE}/snapshots/{_now().strftime('%Y-%m-%d')}.jsonl", {
        "at": _now().isoformat(),
        "analysed_at": dashboard.get("updated_at"),
        "market_sentiment": dashboard.get("market_sentiment"),
        "active_coins": dashboard.get("active_coins"),
        "coins": coins,
    })


def record_equity(state, dca_dashboard):
    coins = (dca_dashboard or {}).get("coins") or {}
    held = {n: {"invested_thb": c.get("total_invested_thb"), "value_thb": c.get("current_value_thb"),
                "pnl_pct": c.get("pnl_pct"), "coin": c.get("total_coin")}
            for n, c in coins.items() if c.get("total_coin")}
    value = (dca_dashboard or {}).get("total_value_thb")
    pool = state.get("cash_pool_thb", 0.0)
    profit = state.get("profit_reserved_thb", 0.0)
    _append(f"{BASE}/equity.jsonl", {
        "at": _now().isoformat(),
        "holdings_invested_thb": (dca_dashboard or {}).get("total_invested_thb"),
        "holdings_value_thb": value,
        "cash_pool_thb": pool,
        "profit_reserved_thb": profit,
        "realized_pnl_total_thb": state.get("realized_pnl_total_thb"),
        "capital_thb": state.get("capital_thb"),
        "withdrawn_total_thb": state.get("withdrawn_total_thb"),
        "total_equity_thb": round((value or 0) + pool + profit, 2) if value is not None else None,
        "held": held,
    })


def record_config(config):
    path = f"{BASE}/config_history.jsonl"
    last = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        if lines:
            last = json.loads(lines[-1]).get("config")
    if last != config:
        _append(path, {"at": _now().isoformat(), "config": config})
