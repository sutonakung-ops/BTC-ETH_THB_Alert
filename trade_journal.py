# -*- coding: utf-8 -*-
"""
สมุดบันทึกการเทรด (trade journal) — เก็บ "สภาพตลาดตอนซื้อ" คู่กับ "ผลลัพธ์ตอนขาย" ของทุกไม้
เพื่อวัด winrate จริง และหาว่าปัจจัยไหนช่วยให้ชนะ (ต้องมีไม้ที่ปิดแล้วพอสมควร ~30-50 ไม้ ถึงจะสรุปได้)

แต่ละไม้ (lot) = การซื้อ 1 ครั้ง ครั้งละ 50 บาท
ตอนขาย บอทขายทั้งก้อนของเหรียญนั้น -> ปิดทุกไม้ที่ยังเปิดของเหรียญนั้นพร้อมกัน
แบ่งเงินที่ได้คืนให้แต่ละไม้ตามสัดส่วนจำนวนเหรียญ แล้วคิดกำไร/ขาดทุนรายไม้
"""

import json
import os
from datetime import datetime, timezone

JOURNAL_FILE = "state/trade_journal.json"
FEATURE_KEYS = ("risk_pct", "signal", "gap_percent", "change_24h",
                "premium_pct", "depth_imbalance", "buy_flow_pct")


def _now():
    return datetime.now(timezone.utc).isoformat()


def load():
    if not os.path.exists(JOURNAL_FILE):
        return {"lots": []}
    try:
        with open(JOURNAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and "lots" in data else {"lots": []}
    except (OSError, ValueError):
        return {"lots": []}


def save(journal):
    os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
    journal["lots"] = journal["lots"][-50000:]
    with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False, indent=2)


def snapshot_features(name, dashboard):
    """สภาพตลาดของเหรียญนี้ + ภาพรวมตลาด ณ ตอนซื้อ (จากผลวิเคราะห์ล่าสุดใน latest.json)"""
    dashboard = dashboard if isinstance(dashboard, dict) else {}
    coin = dashboard.get(name) or {}
    btc = dashboard.get("BTC") or {}
    return {
        "coin": {k: coin.get(k) for k in FEATURE_KEYS},
        "market": {
            "market_sentiment": dashboard.get("market_sentiment"),
            "btc_signal": btc.get("signal"),
            "btc_change_24h": btc.get("change_24h"),
            "analysed_at": dashboard.get("updated_at"),
        },
    }


def backfill_from_state(journal, state):
    """ครั้งแรกที่เริ่มใช้: สร้างไม้จากประวัติการซื้อเดิม (ไม่มีข้อมูลสภาพตลาดตอนนั้น)"""
    if journal["lots"]:
        return
    held = {n for n, h in (state.get("holdings") or {}).items() if (h or {}).get("total_coin", 0) > 0}
    for e in state.get("log") or []:
        if e.get("name") in held:
            journal["lots"].append({
                "lot_id": e.get("order_id") or f"{e['name']}-{e.get('date')}-{len(journal['lots'])}",
                "coin": e["name"], "opened_at": e.get("date"), "invested_thb": e.get("invested_thb"),
                "fill_price": e.get("fill_price"), "amount_coin": e.get("amount_coin"),
                "features": None, "note": "ซื้อก่อนเริ่มบันทึกสภาพตลาด", "status": "open",
            })


def record_buy(journal, name, order_id, invested_thb, fill_price, amount_coin, dashboard, rebuy=False,
               gc_priority=False):
    journal["lots"].append({
        "lot_id": order_id, "coin": name, "opened_at": _now(), "invested_thb": invested_thb,
        "fill_price": fill_price, "amount_coin": amount_coin, "rebuy": rebuy, "gc_priority": gc_priority,
        "features": snapshot_features(name, dashboard), "status": "open",
    })


def record_fill(journal, order_id, fill_price, amount_coin):
    """คำสั่งซื้อที่รอยืนยัน ได้ราคาจริงแล้ว"""
    for lot in journal["lots"]:
        if lot.get("lot_id") == order_id and lot.get("fill_price") is None:
            lot["fill_price"], lot["amount_coin"] = fill_price, amount_coin


def record_close(journal, name, sell_price, proceeds_thb, reason, pnl_pct_at_trigger):
    lots = [l for l in journal["lots"] if l["coin"] == name and l["status"] == "open"]
    total_coin = sum(l.get("amount_coin") or 0 for l in lots)
    for lot in lots:
        share = (lot.get("amount_coin") or 0) / total_coin if total_coin else 1 / len(lots)
        lot_proceeds = proceeds_thb * share
        pnl = lot_proceeds - (lot.get("invested_thb") or 0)
        lot.update({
            "status": "closed", "closed_at": _now(), "exit_reason": reason, "exit_price": sell_price,
            "proceeds_thb": round(lot_proceeds, 4), "pnl_thb": round(pnl, 4),
            "pnl_pct": round(pnl / lot["invested_thb"] * 100, 3) if lot.get("invested_thb") else None,
            "position_pnl_pct_at_trigger": round(pnl_pct_at_trigger, 3),
            "win": pnl > 0,
        })


def summary(journal):
    closed = [l for l in journal["lots"] if l["status"] == "closed" and l.get("pnl_pct") is not None]
    wins = [l for l in closed if l["win"]]
    losses = [l for l in closed if not l["win"]]
    def avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None
    by_reason = {}
    for l in closed:
        by_reason[l.get("exit_reason")] = by_reason.get(l.get("exit_reason"), 0) + 1
    return {
        "open_lots": len([l for l in journal["lots"] if l["status"] == "open"]),
        "closed_lots": len(closed),
        "wins": len(wins),
        "winrate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win_pct": avg([l["pnl_pct"] for l in wins]),
        "avg_loss_pct": avg([l["pnl_pct"] for l in losses]),
        "expectancy_pct": avg([l["pnl_pct"] for l in closed]),
        "total_pnl_thb": round(sum(l["pnl_thb"] for l in closed), 2) if closed else 0.0,
        "by_exit_reason": by_reason,
        "by_entry_trend": _by_entry_trend(closed),
    }


def _by_entry_trend(closed):
    """เทียบผล: ไม้ที่ซื้อตอนแนวโน้ม Golden Cross vs ไม้อื่น (ดูจาก signal ที่บันทึกตอนซื้อ)"""
    groups = {"golden_cross": [], "other": []}
    for l in closed:
        sig = str(((l.get("features") or {}).get("coin") or {}).get("signal") or "")
        if not l.get("features"):
            continue
        groups["golden_cross" if "Golden Cross" in sig else "other"].append(l)
    out = {}
    for k, ls in groups.items():
        wins = [l for l in ls if l["win"]]
        out[k] = {
            "closed": len(ls),
            "winrate_pct": round(len(wins) / len(ls) * 100, 1) if ls else None,
            "expectancy_pct": round(sum(l["pnl_pct"] for l in ls) / len(ls), 2) if ls else None,
        }
    return out
