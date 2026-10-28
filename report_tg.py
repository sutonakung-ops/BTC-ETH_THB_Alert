# -*- coding: utf-8 -*-
"""
รายงานผลบอทเข้า Telegram (ข้อมูลในเครื่องที่รันบอทเท่านั้น — ใช้หลัง tools/vault.py unlock)

เรียกได้ 2 ทาง:
- ผู้ใช้พิมพ์ "รายงาน" ใน Telegram -> crypto_alert บันทึกคำขอ -> price_watch ส่งรายงานภายใน ~1 นาที
- workflow "Send report to Telegram" (สั่งจาก GitHub ได้ตลอด): python report_tg.py
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

REQUEST_FILE = "state/report_request.json"
BKK = timezone(timedelta(hours=7))


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _bkk(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BKK)
    except (AttributeError, ValueError):
        return None


def build_report(ticker=None):
    state = _load("state/dca_state.json", {})
    latest = _load("latest.json", {})

    def price(name):
        t = (ticker or {}).get(f"THB_{name}") if isinstance(ticker, dict) else None
        if isinstance(t, dict) and t.get("last"):
            return float(t["last"])
        return (latest.get(name) or {}).get("price")

    lines = []
    when = datetime.now(BKK).strftime("%d/%m %H:%M")
    src = "ราคาสด" if ticker else "ราคารอบวิเคราะห์ล่าสุด"
    lines.append(f"📊 รายงานผล ({when} น. · {src})")

    value, rows = 0.0, []
    for n, h in sorted((state.get("holdings") or {}).items(), key=lambda kv: -(kv[1] or {}).get("total_invested_thb", 0)):
        if not (h or {}).get("total_coin"):
            continue
        px = price(n)
        if not px:
            rows.append(f"{n}: ทุน {h['total_invested_thb']:,.2f} · ไม่มีราคา")
            continue
        v = h["total_coin"] * px
        value += v
        pnl = v - h["total_invested_thb"]
        sig = (latest.get(n) or {}).get("signal") or ""
        sig = "GC" if "Golden" in sig else ("DC" if "Death" in sig else "-")
        rows.append(f"{n}: {h['total_invested_thb']:,.2f} → {v:,.2f} ({pnl:+.2f} / {pnl / h['total_invested_thb'] * 100:+.1f}%) {sig}")
    dips = []
    for n, d in (state.get("dip_positions") or {}).items():
        if not d.get("coin"):
            continue
        px = price(n)
        if not px:
            continue
        v = d["coin"] * px
        value += v
        pnl = v - d["invested_thb"]
        dips.append(f"{n} (ช้อน): {d['invested_thb']:,.2f} → {v:,.2f} ({pnl:+.2f} / {pnl / d['invested_thb'] * 100:+.1f}%)")

    pool = state.get("cash_pool_thb", 0.0)
    equity = value + pool
    cap = state.get("capital_thb")
    if cap:
        lines.append(f"ทุน {cap:,.2f} → มูลค่า {equity:,.2f} บาท")
        lines.append(f"กำไร/ขาดทุนเทียบทุน: {equity - cap:+,.2f} บาท ({(equity / cap - 1) * 100:+.2f}%)")
    else:
        lines.append(f"มูลค่าพอร์ต {equity:,.2f} บาท")
    lines.append(f"เงินบาทในกอง {pool:,.2f} · ขายแล้วสะสม {state.get('realized_pnl_total_thb', 0.0):+,.2f} บาท")

    lines.append("\nกองปกติ:")
    lines += rows or ["-"]
    lines.append("\nกองช้อน:")
    lines += dips or ["ไม่มีไม้เปิด"]

    # ซื้อขาย 48 ชม. ล่าสุด
    since = datetime.now(timezone.utc) - timedelta(hours=48)
    recent = []
    for day in range(3):
        d = (datetime.now(timezone.utc) - timedelta(days=day)).strftime("%Y-%m-%d")
        for e in _jsonl(f"analysis/events/{d}.jsonl"):
            if e.get("kind") in ("buy", "sell") and (_bkk(e.get("at")) or datetime.min.replace(tzinfo=BKK)) >= since:
                t = _bkk(e["at"]).strftime("%d/%m %H:%M")
                if e["kind"] == "sell":
                    pnl = (e.get("proceeds_thb") or 0) - (e.get("invested_thb") or 0)
                    recent.append((e["at"], f"{t} ขาย {e.get('coin')} ({e.get('reason')}) {pnl:+.2f} บาท"))
                else:
                    tag = " [ช้อน]" if e.get("pool") == "dip" else ""
                    recent.append((e["at"], f"{t} ซื้อ {e.get('coin')} {e.get('thb')} บาท{tag}"))
    lines.append("\nซื้อขาย 48 ชม.:")
    recent = [x for _, x in sorted(recent)]
    if len(recent) > 15:
        recent = [f"(อีก {len(recent) - 15} รายการก่อนหน้า)"] + recent[-15:]
    lines += recent or ["ไม่มี"]

    sh = _load("analysis/optimizer/shadow.json", {})
    tuned = (_load("state/tuned_params.json", {}) or {}).get("params")
    if sh.get("params") or tuned:
        lines.append(f"\nระบบปรับกติกา: ทดลองเงียบชนะ {sh.get('streak', 0)}/3 สัปดาห์" + (" · มีค่าที่ปล่อยใช้แล้ว" if tuned else ""))

    hist = [e for e in _jsonl("analysis/equity.jsonl") if e.get("capital_thb") and not e.get("invalid")]
    if len(hist) > 24:
        prev = hist[-25]
        lines.append(f"เทียบ 24 ชม. ก่อน: {prev['total_equity_thb'] - prev['capital_thb']:+.2f} → {equity - (cap or 0):+.2f} บาท")
    return "\n".join(lines)


def send(text):
    import requests
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(text)
        return False
    for i in range(0, len(text), 3800):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": "🧱 Steady Stack\n" + text[i:i + 3800]}, timeout=15)
    return True


def handle_request(ticker=None):
    """price_watch เรียกทุกนาที: ถ้ามีคำขอ "รายงาน" ค้างอยู่ ส่งรายงานแล้วลบคำขอ คืน True ถ้าส่ง"""
    if not os.path.exists(REQUEST_FILE):
        return False
    try:
        send(build_report(ticker))
    finally:
        os.remove(REQUEST_FILE)
    return True


if __name__ == "__main__":
    ticker = None
    try:
        import requests
        ticker = requests.get("https://api.bitkub.com/api/market/ticker", timeout=20).json()
    except Exception:  # noqa: BLE001
        ticker = None
    send(build_report(ticker))
