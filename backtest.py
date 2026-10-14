# -*- coding: utf-8 -*-
"""
ทดสอบกลยุทธ์ย้อนหลัง (backtest) ด้วยข้อมูลราคาจริงจาก Bitkub
ใช้สูตรคำนวณชุดเดียวกับ trading_bot.py เป๊ะๆ (import จาก strategy.py)
เพื่อให้ผลทดสอบสะท้อนพฤติกรรมจริงของบอท ไม่ใช่กลยุทธ์คนละตัว

วิธีรัน (ผ่าน GitHub Actions workflow_dispatch):
  python backtest.py --coin BTC --days 180 --stop-loss 4 --take-profit 6 --rsi-overbought 70

ผลลัพธ์จะพิมพ์สรุปออกมา และเขียนไฟล์ backtest_result.json ไว้ให้ดาวน์โหลดเป็น artifact
"""

import argparse
import json
import time

import requests

from strategy import check_exit, compute_indicators, is_entry_signal

CANDLE_URL = "https://api.bitkub.com/tradingview/history"
FEE_PCT = 0.25  # ค่าธรรมเนียม Bitkub ต่อฝั่ง (ซื้อ 0.25% + ขาย 0.25%)

COIN_SYMBOLS = {
    "BTC": "BTC_THB",
    "ETH": "ETH_THB",
}


def fetch_candles_chunked(symbol, resolution_minutes, start_ts, end_ts, chunk_days=30):
    """ดึงแท่งเทียนย้อนหลังเป็นช่วงๆ (กันเผื่อ API จำกัดช่วงเวลาต่อ request)"""
    merged = {"c": [], "h": [], "l": [], "o": [], "t": [], "v": []}
    chunk_seconds = chunk_days * 86400
    cur = start_ts
    while cur < end_ts:
        chunk_end = min(cur + chunk_seconds, end_ts)
        params = {"symbol": symbol, "resolution": str(resolution_minutes), "from": cur, "to": chunk_end}
        resp = requests.get(CANDLE_URL, params=params, timeout=30)
        data = resp.json()
        if data.get("s") == "ok" and data.get("t"):
            for key in merged:
                merged[key].extend(data.get(key, []))
        cur = chunk_end
        time.sleep(0.25)  # กันยิง API ถี่เกินไป

    # กันแท่งซ้ำตรงรอยต่อของแต่ละ chunk
    seen = set()
    keep_idx = []
    for i, t in enumerate(merged["t"]):
        if t not in seen:
            seen.add(t)
            keep_idx.append(i)
    deduped = {k: [v[i] for i in keep_idx] for k, v in merged.items()}
    return deduped


def run_backtest(candles, capital_thb=1000.0, stop_loss_pct=4.0, take_profit_pct=6.0,
                  rsi_overbought=70, ma_gap_threshold=0.3, warmup=24,
                  require_uptrend=False, trend_ma_period=50):
    closes, volumes, times = candles["c"], candles["v"], candles["t"]

    if require_uptrend:
        warmup = max(warmup, trend_ma_period)

    position = None
    trades = []
    cum_pnl = 0.0
    equity_curve = []  # กำไร/ขาดทุนสะสม (บาท) ตามเวลา ไว้คำนวณ drawdown

    for i in range(warmup, len(closes)):
        window_closes = closes[: i + 1]
        window_volumes = volumes[: i + 1]
        indicators = compute_indicators(
            window_closes, window_volumes,
            trend_ma_period=trend_ma_period if require_uptrend else None,
        )

        if position is None:
            if is_entry_signal(indicators, ma_gap_threshold, rsi_overbought, require_uptrend=require_uptrend):
                position = {
                    "entry_price": indicators["price"],
                    "entry_time": times[i],
                }
        else:
            reason, change_pct = check_exit(
                indicators, position["entry_price"], stop_loss_pct, take_profit_pct, ma_gap_threshold
            )
            if reason:
                exit_price = indicators["price"]
                gross_pct = (exit_price / position["entry_price"] - 1) * 100
                fee_pct_total = FEE_PCT * 2  # ซื้อ + ขาย
                net_pct = gross_pct - fee_pct_total
                pnl_thb = capital_thb * net_pct / 100
                cum_pnl += pnl_thb
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": times[i],
                    "entry_price": round(position["entry_price"], 4),
                    "exit_price": round(exit_price, 4),
                    "pnl_thb": round(pnl_thb, 2),
                    "pnl_pct": round(net_pct, 2),
                    "reason": reason,
                })
                position = None
        equity_curve.append(cum_pnl)

    return {"trades": trades, "final_pnl_thb": round(cum_pnl, 2), "equity_curve": equity_curve}


def summarize(result, capital_thb):
    trades = result["trades"]
    if not trades:
        return {
            "total_trades": 0,
            "win_rate_pct": 0,
            "total_pnl_thb": 0,
            "total_return_pct": 0,
            "avg_win_thb": 0,
            "avg_loss_thb": 0,
            "profit_factor": None,
            "max_drawdown_thb": 0,
        }

    wins = [t for t in trades if t["pnl_thb"] > 0]
    losses = [t for t in trades if t["pnl_thb"] <= 0]
    total_pnl = sum(t["pnl_thb"] for t in trades)
    gross_profit = sum(t["pnl_thb"] for t in wins)
    gross_loss = -sum(t["pnl_thb"] for t in losses)

    peak = 0.0
    max_dd = 0.0
    for e in result["equity_curve"]:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)

    return {
        "total_trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "total_pnl_thb": round(total_pnl, 2),
        "total_return_pct": round(total_pnl / capital_thb * 100, 2),
        "avg_win_thb": round(gross_profit / len(wins), 2) if wins else 0,
        "avg_loss_thb": round(-gross_loss / len(losses), 2) if losses else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_thb": round(max_dd, 2),
    }


def sweep(candles, capital_thb, stop_loss_grid, take_profit_grid, rsi_grid, ma_gap=0.3,
          trend_filter_grid=((False, None),)):
    """
    ลองหลายชุดพารามิเตอร์กับข้อมูลเดียวกัน (ดึงราคาแค่ครั้งเดียว) แล้วจัดอันดับผลลัพธ์
    trend_filter_grid: list ของ (require_uptrend, trend_ma_period) ที่จะลอง เช่น
      [(False, None), (True, 50), (True, 100)]
    """
    rows = []
    for sl in stop_loss_grid:
        for tp in take_profit_grid:
            for rsi in rsi_grid:
                for require_uptrend, trend_ma_period in trend_filter_grid:
                    result = run_backtest(
                        candles, capital_thb=capital_thb, stop_loss_pct=sl,
                        take_profit_pct=tp, rsi_overbought=rsi, ma_gap_threshold=ma_gap,
                        require_uptrend=require_uptrend, trend_ma_period=trend_ma_period or 50,
                    )
                    summary = summarize(result, capital_thb)
                    rows.append({
                        "stop_loss_pct": sl, "take_profit_pct": tp, "rsi_overbought": rsi,
                        "trend_filter": f"MA{trend_ma_period}" if require_uptrend else "off",
                        **summary,
                    })
    rows.sort(key=lambda r: r["total_return_pct"], reverse=True)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin", default="BTC,ETH", help="BTC, ETH, หรือ BTC,ETH")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--offset-days", type=int, default=0,
                         help="เลื่อนช่วงเวลาย้อนหลังออกไปอีกกี่วัน (ใช้ทดสอบ out-of-sample บนช่วงเก่ากว่า)")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--stop-loss", type=float, default=4.0)
    parser.add_argument("--take-profit", type=float, default=6.0)
    parser.add_argument("--rsi-overbought", type=float, default=70.0)
    parser.add_argument("--ma-gap", type=float, default=0.3)
    parser.add_argument("--sweep", action="store_true", help="ลองหลายชุดพารามิเตอร์แล้วจัดอันดับ แทนการรันค่าเดียว")
    parser.add_argument("--resolution", default="60", help="ความละเอียดแท่งเทียนเป็นนาที เช่น 60 (1ชม.), 240 (4ชม.)")
    parser.add_argument("--require-uptrend", action="store_true", help="เปิด trend filter (เข้าซื้อเฉพาะตอนราคาอยู่เหนือ trend MA)")
    parser.add_argument("--trend-ma", type=int, default=50, help="คาบของ trend MA (ใช้ตอน --require-uptrend)")
    args = parser.parse_args()

    coins = [c.strip().upper() for c in args.coin.split(",") if c.strip()]
    now = int(time.time()) - args.offset_days * 86400
    start = now - args.days * 86400

    all_results = {}
    for coin in coins:
        symbol = COIN_SYMBOLS.get(coin)
        if not symbol:
            print(f"ข้ามเหรียญที่ไม่รู้จัก: {coin}")
            continue

        print(f"กำลังดึงข้อมูลย้อนหลัง {args.days} วันของ {coin} (แท่ง {args.resolution} นาที) ...")
        candles = fetch_candles_chunked(symbol, args.resolution, start, now)
        print(f"  ได้ {len(candles['t'])} แท่งเทียน (รายชั่วโมง)")

        if len(candles["t"]) < 48:
            print(f"  ข้อมูลไม่พอสำหรับทดสอบ ({coin})")
            continue

        if args.sweep:
            rows = sweep(
                candles, args.capital,
                stop_loss_grid=[3, 4, 5],
                take_profit_grid=[6, 8, 10],
                rsi_grid=[70, 75],
                ma_gap=args.ma_gap,
                trend_filter_grid=[(False, None), (True, 50), (True, 100)],
            )
            all_results[coin] = {"sweep_top10": rows[:10], "sweep_all": rows}
            print(f"\n=== TOP 5 พารามิเตอร์ {coin} ({args.days} วัน, จัดอันดับตาม total_return_pct) ===")
            for r in rows[:5]:
                print(json.dumps(r, ensure_ascii=False))
        else:
            result = run_backtest(
                candles,
                capital_thb=args.capital,
                stop_loss_pct=args.stop_loss,
                take_profit_pct=args.take_profit,
                rsi_overbought=args.rsi_overbought,
                ma_gap_threshold=args.ma_gap,
                require_uptrend=args.require_uptrend,
                trend_ma_period=args.trend_ma,
            )
            summary = summarize(result, args.capital)
            all_results[coin] = {"summary": summary, "trades": result["trades"]}
            print(f"\n=== ผลทดสอบย้อนหลัง {coin} ({args.days} วัน) ===")
            print(json.dumps(summary, indent=2, ensure_ascii=False))

    output = {
        "mode": "sweep" if args.sweep else "single",
        "params": {
            "days": args.days,
            "capital_thb": args.capital,
            "stop_loss_pct": args.stop_loss,
            "take_profit_pct": args.take_profit,
            "rsi_overbought": args.rsi_overbought,
            "ma_gap_threshold": args.ma_gap,
            "fee_pct_per_side": FEE_PCT,
        },
        "results": all_results,
    }
    with open("backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print("\nบันทึกผลละเอียดไว้ที่ backtest_result.json")


if __name__ == "__main__":
    main()
