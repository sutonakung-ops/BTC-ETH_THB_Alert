# -*- coding: utf-8 -*-
"""
ระบบปรับกติกาเอง แบบ "ทดลองเงียบ แล้วค่อยปล่อย" (ผู้ใช้เลือกระดับ 2, 28 ต.ค. 2569) — รันสัปดาห์ละครั้ง

ทุกสัปดาห์:
1) ดึงกราฟรายชั่วโมงของ Bitkub ย้อนหลัง LOOKBACK_DAYS วัน ของเหรียญที่บอทเกี่ยวข้อง
2) ให้คะแนน "ค่าที่ทดลองเงียบ (shadow)" กับ "ค่าที่ใช้จริง (live)" จากผลของ 7 วันล่าสุด (ข้อมูลที่ไม่เคยใช้หาค่ามาก่อน)
   -> นับว่า shadow ชนะ live กี่สัปดาห์
3) ถ้า shadow ชนะติดกัน PROMOTE_WEEKS สัปดาห์ และชนะรวมทั้งช่วงทดลอง -> ใช้ค่าจริง (เขียน state/tuned_params.json)
4) หาค่าที่ดีที่สุดใหม่ (ข้อมูลช่วงแรกหาค่า แล้วต้องดีกว่าชัดเจนในช่วงหลังด้วย) -> เป็น shadow สัปดาห์หน้า
   (ถ้า shadow ตัวใหม่ต่างจากตัวเดิม เริ่มนับสัปดาห์ชนะใหม่)
5) สรุปทาง Telegram + บันทึกทุกอย่างไว้ใน analysis/optimizer/

ค่าที่ปรับได้ถูกล็อกกรอบไว้ใน BOUNDS (dca_bot.py ก็ clamp ซ้ำอีกชั้น) · ขนาดไม้/สัดส่วนกองช้อน ไม่ให้ปรับเอง
"""
import json
import os
import statistics as st
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.getcwd())

LOOKBACK_DAYS = 150
FORWARD_DAYS = 7
PROMOTE_WEEKS = 3          # shadow ต้องชนะ live ติดกันกี่สัปดาห์ถึงปล่อยใช้จริง
MIN_IMPROVEMENT = 0.10     # ค่าใหม่ต้องดีกว่าค่าปัจจุบันอย่างน้อย 10% ทั้งช่วงหาค่าและช่วงทดสอบ
FEE = 0.005
OUT_DIR = "analysis/optimizer"
SHADOW_FILE = f"{OUT_DIR}/shadow.json"
HISTORY_FILE = f"{OUT_DIR}/history.jsonl"
TUNED_FILE = "state/tuned_params.json"
BOUNDS = {
    "TAKE_PROFIT_PCT": (4.0, 10.0),
    "STOP_LOSS_PCT": (15.0, 25.0),
    "CUT_ZONE_PCT": (8.0, 15.0),
    "DIP_DROP_PCT": (10.0, 20.0),
    "DIP_TP_PCT": (4.0, 10.0),
}
GRID_MAIN = {"TAKE_PROFIT_PCT": [4, 5, 6, 7, 8, 10], "CUT_ZONE_PCT": [8, 10, 12, 15], "STOP_LOSS_PCT": [15, 20, 25]}
GRID_DIP = {"DIP_DROP_PCT": [10, 12, 15, 18, 20], "DIP_TP_PCT": [4, 6, 8, 10]}
BASE_COINS = ["BTC", "ETH", "XRP", "DOGE", "SOL", "NEAR", "WLD", "CRV", "OP", "ADA", "XLM", "KUB"]


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def clamp(params):
    out = {}
    for k, v in params.items():
        if k in BOUNDS:
            lo, hi = BOUNDS[k]
            out[k] = float(min(max(float(v), lo), hi))
    return out


# ---------------- ข้อมูล ----------------
def coin_universe():
    latest = load("latest.json", {})
    state = load("state/dca_state.json", {})
    names = set(BASE_COINS) | set(latest.get("active_coins") or [])
    names |= {n for n, h in (state.get("holdings") or {}).items() if (h or {}).get("total_coin", 0) > 0}
    names |= set(state.get("dip_positions") or {})
    return sorted(names)


def fetch(coins, days=LOOKBACK_DAYS):
    now = int(time.time())
    data = {}
    for c in coins:
        try:
            d = requests.get("https://api.bitkub.com/tradingview/history",
                             params={"symbol": f"{c}_THB", "resolution": "60", "from": now - days * 86400, "to": now},
                             timeout=30).json()
            if d.get("c") and len(d["c"]) > 200:
                data[c] = {k: [float(x) for x in d[k]] for k in ("t", "h", "l", "c")}
        except (requests.RequestException, ValueError, KeyError, TypeError):
            pass
    return data


# ---------------- จำลอง ----------------
def _ma(cl, n):
    out, s = [None] * len(cl), 0.0
    for i, x in enumerate(cl):
        s += x
        if i >= n:
            s -= cl[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def prep(data):
    for v in data.values():
        cl = v["c"]
        v["ma12"], v["ma24"] = _ma(cl, 12), _ma(cl, 24)
        vol = [None] * len(cl)
        for i in range(48, len(cl)):
            w = cl[i - 47:i + 1]
            m = sum(w) / 48
            vol[i] = st.pstdev(w) / m * 100
        v["vol48"] = vol
    return data


def sim_main(v, p, start, end, maxh=24 * 30, mtm=False):
    """กองปกติ: ซื้อได้ทุกชั่วโมงเมื่อไม่มีไม้ค้าง · ขาย TP / ตัดเมื่อขาดทุนเกิน cut และ MA12<MA24 / SL"""
    h, l, cl, m12, m24 = v["h"], v["l"], v["c"], v["ma12"], v["ma24"]
    tp, sl, cz = p["TAKE_PROFIT_PCT"] / 100, p["STOP_LOSS_PCT"] / 100, p["CUT_ZONE_PCT"] / 100
    out, i = [], max(start, 24)
    while i < min(end, len(cl) - 1):
        e, j, r = cl[i], i + 1, None
        while j < len(cl) and j - i <= maxh:
            if l[j] <= e * (1 - sl):
                r = -sl; break
            if h[j] >= e * (1 + tp):
                r = tp; break
            if cl[j] <= e * (1 - cz) and m12[j] and m24[j] and (m12[j] - m24[j]) / m24[j] * 100 < -0.3:
                r = cl[j] / e - 1; break
            j += 1
        if r is None:
            if j >= len(cl):
                if not mtm:
                    break
                r = cl[-1] / e - 1  # ไม้ที่ยังไม่ปิด: คิดตามราคาล่าสุด (ไม่งั้นค่า TP ต่ำได้เปรียบ)
                out.append((r - FEE, max(j - 1 - i, 1)))
                break
            r = cl[j - 1] / e - 1
        out.append((r - FEE, j - i))
        i = j + 1
    return out


def sim_dip(v, p, start, end, maxh=24 * 30, sl=0.20, max_vol=8.0, mtm=False):
    h, l, cl, vol = v["h"], v["l"], v["c"], v["vol48"]
    drop, tp = p["DIP_DROP_PCT"] / 100, p["DIP_TP_PCT"] / 100
    out, i = [], max(start, 72)
    while i < min(end, len(cl) - 1):
        if cl[i] <= max(h[i - 72:i + 1]) * (1 - drop) and vol[i] is not None and vol[i] <= max_vol:
            e, j, r = cl[i], i + 1, None
            while j < len(cl) and j - i <= maxh:
                if l[j] <= e * (1 - sl):
                    r = -sl; break
                if h[j] >= e * (1 + tp):
                    r = tp; break
                j += 1
            if r is None:
                if j >= len(cl):
                    if mtm:
                        out.append((cl[-1] / e - 1 - FEE, max(j - 1 - i, 1)))
                    break
                r = cl[j - 1] / e - 1
            out.append((r - FEE, j - i))
            i = j + 1
        else:
            i += 1
    return out


def score(trades):
    """คะแนน = กำไรต่อวันที่เงินถูกใช้ (%), จำนวนไม้, winrate, เฉลี่ยต่อไม้"""
    if not trades:
        return {"per_day": None, "n": 0, "win": None, "avg": None}
    rets = [r for r, _ in trades]
    hrs = sum(hh for _, hh in trades)
    return {"per_day": round(sum(rets) / (hrs / 24) * 100, 4) if hrs else None, "n": len(trades),
            "win": round(sum(r > 0 for r in rets) / len(rets) * 100, 1), "avg": round(st.mean(rets) * 100, 3)}


def evaluate(data, params, kind, frac=(0.0, 1.0), last_hours=None):
    trades = []
    for v in data.values():
        n = len(v["c"])
        if last_hours:
            s, e = max(n - last_hours, 0), n
        else:
            s, e = int(n * frac[0]), int(n * frac[1])
        trades += (sim_main if kind == "main" else sim_dip)(v, params, s, e, mtm=bool(last_hours))
    return score(trades)


def grid(g):
    keys = list(g)
    combos = [{}]
    for k in keys:
        combos = [dict(c, **{k: float(x)}) for c in combos for x in g[k]]
    return combos


def best_candidate(data, live, kind):
    g = GRID_MAIN if kind == "main" else GRID_DIP
    keys = list(g)
    live_k = {k: live[k] for k in keys}
    base_tr = evaluate(data, live_k, kind, (0.0, 0.67))
    base_te = evaluate(data, live_k, kind, (0.67, 1.0))
    ranked = []
    for c in grid(g):
        tr = evaluate(data, c, kind, (0.0, 0.67))
        if tr["per_day"] is None or tr["n"] < 20:
            continue
        ranked.append((tr["per_day"], c, tr))
    ranked.sort(key=lambda x: -x[0])
    for _, c, tr in ranked[:5]:  # ตัวท็อปช่วงหาค่า ต้องผ่านช่วงทดสอบด้วย
        te = evaluate(data, c, kind, (0.67, 1.0))
        if te["per_day"] is None or te["n"] < 8:
            continue
        better = lambda new, old: old is None or (new > old * (1 + MIN_IMPROVEMENT) if old > 0 else new > old + abs(old) * MIN_IMPROVEMENT)
        if better(tr["per_day"], base_tr["per_day"]) and better(te["per_day"], base_te["per_day"]):
            return c, {"train": tr, "test": te, "live_train": base_tr, "live_test": base_te}
    return live_k, {"train": base_tr, "test": base_te, "live_train": base_tr, "live_test": base_te, "no_better": True}


# ---------------- Telegram ----------------
def tg(msg):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(msg)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": "🧱 Steady Stack\n" + msg}, timeout=15)
    except requests.RequestException:
        pass


# ---------------- หลัก ----------------
def live_params():
    import importlib
    os.environ.setdefault("TELEGRAM_TOKEN", "x")
    os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
    dca = importlib.import_module("dca_bot")
    return {k: float(getattr(dca, k)) for k in BOUNDS}


def run(data=None, live=None, now=None):
    now = now or datetime.now(timezone.utc)
    live = live or live_params()
    data = prep(data or fetch(coin_universe()))
    shadow = load(SHADOW_FILE, {})
    fwd_h = FORWARD_DAYS * 24
    lines, promoted = [], False

    # 1) ผลจริงสัปดาห์ที่ผ่านมา (forward) — shadow vs live
    if shadow.get("params"):
        sp = shadow["params"]
        res = {}
        for kind, keys in (("main", GRID_MAIN), ("dip", GRID_DIP)):
            s_sc = evaluate(data, {k: sp[k] for k in keys}, kind, last_hours=fwd_h)
            l_sc = evaluate(data, {k: live[k] for k in keys}, kind, last_hours=fwd_h)
            res[kind] = {"shadow": s_sc, "live": l_sc}
        diff = lambda kind: ((res[kind]["shadow"]["per_day"] or 0) - (res[kind]["live"]["per_day"] or 0))
        same = all(abs(sp[k] - live[k]) < 1e-9 for k in BOUNDS)
        won = (not same) and (diff("main") + diff("dip")) > 0
        shadow.setdefault("weeks", []).append({"at": now.isoformat(), "won": won, "forward": res})
        shadow["streak"] = (shadow.get("streak", 0) + 1) if won else 0
        lines.append(f"สัปดาห์ที่ผ่านมา: ค่าทดลอง{'ชนะ' if won else 'ไม่ชนะ'}ค่าจริง (ชนะติดกัน {shadow['streak']}/{PROMOTE_WEEKS})")
        # 2) ปล่อยใช้จริง
        total = sum(1 if w["won"] else -1 for w in shadow["weeks"][-PROMOTE_WEEKS:])
        if shadow["streak"] >= PROMOTE_WEEKS and total > 0 and not same:
            tuned = clamp(sp)
            save(TUNED_FILE, {"params": tuned, "applied_at": now.isoformat(), "from": "optimizer",
                              "previous": live, "evidence": shadow["weeks"][-PROMOTE_WEEKS:]})
            changes = ", ".join(f"{k} {live[k]:g}→{tuned[k]:g}" for k in tuned if abs(tuned[k] - live[k]) > 1e-9)
            lines.append(f"✅ ปล่อยใช้จริงแล้ว: {changes}")
            live = dict(live, **tuned)
            promoted = True
            shadow = {}

    # 3) หาค่าทดลองตัวใหม่
    cand, ev = {}, {}
    for kind in ("main", "dip"):
        c, e = best_candidate(data, live, kind)
        cand.update(c)
        ev[kind] = e
    cand = clamp(dict(live, **cand))
    changed_vs_shadow = shadow.get("params") != cand
    if changed_vs_shadow:
        shadow = {"params": cand, "since": now.isoformat(), "streak": 0, "weeks": [], "evidence": ev}
    else:
        shadow["evidence"] = ev
    save(SHADOW_FILE, shadow)

    diffs = [k for k in BOUNDS if abs(cand[k] - live[k]) > 1e-9]
    if diffs:
        desc = ", ".join(f"{k} {live[k]:g}→{cand[k]:g}" for k in diffs)
        lines.append(f"ค่าที่กำลังทดลองเงียบ: {desc}" + (" (ตัวใหม่ เริ่มนับใหม่)" if changed_vs_shadow else ""))
    else:
        lines.append("ยังไม่เจอค่าที่ดีกว่าค่าปัจจุบันชัดเจน — ใช้ค่าเดิมต่อ")
    for kind, lab in (("main", "กองปกติ"), ("dip", "กองช้อน")):
        e = ev[kind]
        lines.append(f"{lab} (ทดสอบย้อนหลังช่วงล่าสุด): ค่าจริง {e['live_test']['per_day']}%/วัน · ค่าทดลอง {e['test']['per_day']}%/วัน")

    rec = {"at": now.isoformat(), "live": live, "candidate": cand, "promoted": promoted,
           "streak": shadow.get("streak", 0), "evidence": ev, "coins": sorted(data)}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    tg("🧠 ระบบปรับกติกา (รายสัปดาห์)\n" + "\n".join(lines))
    return rec


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=1)[:3000])
