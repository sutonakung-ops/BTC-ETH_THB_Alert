# -*- coding: utf-8 -*-
"""
EFDS — External Factor Defense System (ผู้ใช้ตั้งชื่อ 12 พ.ย. 2569)

ตรวจจับตลาดผิดปกติจากปัจจัยภายนอก แล้วหยุดซื้อใหม่ชั่วคราว (ทั้งกองปกติและกองช้อน) — การขายตามกติกายังทำงานปกติ

ตัวจับ (ปรับจากข้อมูลจริงของ Bitkub 180 วัน, 13 เหรียญ, ราย ชม.):
- BTC ลงตั้งแต่ 4% ใน 24 ชม. -> ซื้อตอนนั้นแล้วถือ 7 วัน เฉลี่ย -5.1% (ซื้อเวลาปกติ +0.9%)
  สัญญาณอื่นที่ทดสอบแล้วไม่ใช้: เหรียญส่วนใหญ่ร่วงพร้อมกัน / ความผันผวนพุ่ง -> หลังจากนั้นมักเด้ง (+3.5% / +4.7%) เป็นจังหวะช้อน ไม่ใช่จังหวะหนี
- เปิดค้างไว้จนผ่าน HOLD_HOURS ชม. หลังสัญญาณล่าสุด: ภายใน 12 ชม. หลังสัญญาณ ซื้อแล้ว 7 วันยัง -1.3%, พ้น 24 ชม. กลับมาเป็นบวก

โหมด:
- auto   : ระบบเปิดเอง / ปิดเองเมื่อพ้น HOLD_HOURS
- manual : ผู้ใช้เปิดเอง (พิมพ์ "EFDS" / "ป้องกัน") — อยู่จนผู้ใช้ปิด
ผู้ใช้สั่งปิดระหว่างที่สัญญาณยังอยู่ -> ระบบจะไม่เปิดเองซ้ำ OVERRIDE_HOURS ชม.

สถานะเก็บใน control.json: control["defensive"] (ค่าที่บอททุกส่วนใช้) + control["efds"] (รายละเอียด)
"""
from datetime import datetime, timedelta, timezone

TRIGGER_BTC_24H_PCT = -4.0
HOLD_HOURS = 24
OVERRIDE_HOURS = 24
NAME = "EFDS (External Factor Defense System)"


def _now():
    return datetime.now(timezone.utc)


def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def btc_change_24h(ticker=None, dashboard=None):
    t = (ticker or {}).get("THB_BTC") if isinstance(ticker, dict) else None
    if isinstance(t, dict) and t.get("percentChange") is not None:
        try:
            return float(t["percentChange"])
        except (TypeError, ValueError):
            pass
    b = (dashboard or {}).get("BTC") if isinstance(dashboard, dict) else None
    if isinstance(b, dict) and b.get("change_24h") is not None:
        return float(b["change_24h"])
    return None


def set_manual(control, on, now=None):
    """คำสั่งจากผู้ใช้ (Telegram / workflow)"""
    now = now or _now()
    e = dict(control.get("efds") or {})
    if on:
        e.update({"active": True, "mode": "manual", "since": now.isoformat(), "reason": "ผู้ใช้เปิดเอง"})
        e.pop("override_until", None)
    else:
        was_auto_signal = e.get("active") and e.get("mode") == "auto"
        e.update({"active": False, "mode": None, "released_at": now.isoformat(), "reason": "ผู้ใช้ปิดเอง"})
        if was_auto_signal or e.get("last_trigger_at"):
            e["override_until"] = (now + timedelta(hours=OVERRIDE_HOURS)).isoformat()
    control["efds"] = e
    control["defensive"] = bool(e["active"])
    return control


def update(control, ticker=None, dashboard=None, now=None):
    """
    เรียกทุกนาที/ทุกชั่วโมง คืน (control, event) — event = None / {"kind": "on"|"off", ...} สำหรับแจ้ง Telegram
    ย้ายค่าเดิม control["defensive"]=True (สั่งเองก่อนมี EFDS) เป็น manual อัตโนมัติ
    """
    now = now or _now()
    e = dict(control.get("efds") or {})
    if not e and control.get("defensive"):
        e = {"active": True, "mode": "manual", "since": now.isoformat(), "reason": "เปิดเองก่อนมี EFDS"}
    chg = btc_change_24h(ticker, dashboard)
    event = None
    firing = chg is not None and chg <= TRIGGER_BTC_24H_PCT
    if firing:  # บันทึกเฉพาะตอนมีสัญญาณ (ไม่งั้นไฟล์เปลี่ยนทุกนาที -> commit ทุกนาที)
        e["last_trigger_at"] = now.replace(minute=0, second=0, microsecond=0).isoformat()  # ละเอียดระดับชั่วโมง
        e["btc_24h"] = round(chg, 2)
    override = _parse(e.get("override_until"))
    overridden = override is not None and now < override
    if firing and not e.get("active") and not overridden:
        e.update({"active": True, "mode": "auto", "since": now.isoformat(),
                  "reason": f"BTC ลง {chg:.1f}% ใน 24 ชม. (เกณฑ์ {TRIGGER_BTC_24H_PCT:g}%)"})
        event = {"kind": "on", "reason": e["reason"]}
    elif e.get("active") and e.get("mode") == "auto":
        last = _parse(e.get("last_trigger_at")) or _parse(e.get("since"))
        if last and now - last >= timedelta(hours=HOLD_HOURS):
            e.update({"active": False, "mode": None, "released_at": now.isoformat(),
                      "reason": f"ไม่มีสัญญาณผิดปกติครบ {HOLD_HOURS} ชม.",
                      "btc_24h": round(chg, 2) if chg is not None else None})
            event = {"kind": "off", "reason": e["reason"]}
    if overridden is False and e.get("override_until"):
        e.pop("override_until", None)
    control["efds"] = e
    control["defensive"] = bool(e.get("active"))
    return control, event


def message(event, control):
    e = control.get("efds") or {}
    if event["kind"] == "on":
        return (f"🛡️ EFDS ทำงาน — ตรวจพบตลาดผิดปกติ: {event['reason']}\n"
                "หยุดซื้อใหม่ชั่วคราว (กองปกติ + กองช้อน) · การขายตามกติกายังทำงานปกติ\n"
                f"จะปิดเองเมื่อไม่มีสัญญาณครบ {HOLD_HOURS} ชม. · พิมพ์ \"ปิด EFDS\" เพื่อปิดเอง")
    return (f"🟢 EFDS ปิดแล้ว — {event['reason']}\nกลับมาซื้อตามกติกาปกติ"
            + (f" (BTC 24 ชม. ตอนนี้ {e['btc_24h']:+.1f}%)" if e.get("btc_24h") is not None else ""))


def status_text(control):
    e = control.get("efds") or {}
    if not control.get("defensive"):
        return "EFDS: ปิด (ตลาดปกติ)"
    mode = "ผู้ใช้เปิดเอง" if e.get("mode") == "manual" else "เปิดอัตโนมัติ"
    return f"🛡️ EFDS ทำงานอยู่ ({mode}): {e.get('reason', '')} — ไม่ซื้อใหม่ ยังขายตามกติกา"
