# -*- coding: utf-8 -*-
"""
ตรรกะกลยุทธ์ล้วนๆ (ไม่มีการเรียก API ซื้อ-ขายหรือ Telegram ในไฟล์นี้)
แยกออกมาเป็นไฟล์กลาง เพื่อให้ trading_bot.py (รันจริง) และ backtest.py
(ทดสอบย้อนหลัง) ใช้สูตรคำนวณชุดเดียวกันเป๊ะๆ — กันไม่ให้ผลย้อนหลังที่ทดสอบ
ไม่ตรงกับพฤติกรรมจริงของบอท
"""


def moving_average(prices, period):
    return sum(prices[-period:]) / period


def calc_rsi(closes, period=14):
    window = closes[-(period + 1):]
    if len(window) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(window)):
        diff = window[i] - window[i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def volume_confirmed(volumes, lookback=24):
    recent = volumes[-lookback:]
    if len(recent) < 2:
        return False
    avg_vol = sum(recent[:-1]) / len(recent[:-1])
    return recent[-1] > avg_vol


def compute_indicators(closes, volumes, rsi_period=14, vol_lookback=24):
    ma12 = moving_average(closes, 12)
    ma24 = moving_average(closes, 24)
    gap_percent = (ma12 - ma24) / ma24 * 100
    return {
        "price": closes[-1],
        "ma12": ma12,
        "ma24": ma24,
        "gap_percent": round(gap_percent, 4),
        "rsi": calc_rsi(closes, rsi_period),
        "vol_confirmed": volume_confirmed(volumes, vol_lookback),
    }


def is_entry_signal(indicators, ma_gap_threshold=0.3, rsi_overbought=70):
    """เงื่อนไขเข้าซื้อ: Golden Cross ชัดเจน + RSI ยังไม่ overbought + ปริมาณซื้อขายยืนยัน"""
    return (
        indicators["gap_percent"] > ma_gap_threshold
        and indicators["rsi"] is not None
        and indicators["rsi"] < rsi_overbought
        and indicators["vol_confirmed"]
    )


def check_exit(indicators, entry_price, stop_loss_pct=4.0, take_profit_pct=6.0, ma_gap_threshold=0.3):
    """คืน (เหตุผลที่ควรปิดไม้ หรือ None, % เปลี่ยนแปลงจากจุดเข้า)"""
    change_pct = (indicators["price"] - entry_price) / entry_price * 100
    if change_pct <= -stop_loss_pct:
        return f"stop-loss ({stop_loss_pct}%)", change_pct
    if change_pct >= take_profit_pct:
        return f"take-profit ({take_profit_pct}%)", change_pct
    if indicators["gap_percent"] < -ma_gap_threshold:
        return "death-cross", change_pct
    return None, change_pct
