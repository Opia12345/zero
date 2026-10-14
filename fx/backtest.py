"""Backtest on Deriv multipliers, measured in R (1R = the dollar amount risked
at the stop-loss). Usage:

    python3 -m fx.backtest [--symbols frxXAUUSD,frxEURUSD] [--days 180] [--cost 0.0002]

Multiplier P&L is stake * multiplier * (price change / entry). Sizing keeps
the loss at the stop-loss equal to 1R, so a trade's notional is
R * entry / sl_dist, and a fee of `cost` (fraction of notional, charged once)
costs  cost * entry / sl_dist  in R. Tight stops are therefore expensive.
That is why the cost is a parameter: confirm Deriv's real commission by
asking for a live proposal before trusting any result.

Fills: entry at the next bar's open; if a bar touches both stop and target
the stop is assumed to hit first (pessimistic). A position still open after
MAX_HOLD bars is closed at that bar's close."""

import argparse

import pandas as pd

from .data import load_candles
from .strategies import STRATEGIES, Signal

MAX_HOLD = 96  # bars (24h on M15)


def simulate(df: pd.DataFrame, signals: list[Signal], cost: float) -> pd.DataFrame:
    trades, free_from = [], 0
    for s in signals:
        e = s.idx + 1
        if e < free_from or e >= len(df):
            continue
        entry = df.open[e]
        up = s.side == "UP"
        sl = entry - s.sl_dist if up else entry + s.sl_dist
        tp = entry + s.tp_dist if up else entry - s.tp_dist
        exit_px, j = df.close[min(e + MAX_HOLD, len(df) - 1)], min(e + MAX_HOLD, len(df) - 1)
        for k in range(e, min(e + MAX_HOLD, len(df) - 1) + 1):
            hit_sl = df.low[k] <= sl if up else df.high[k] >= sl
            hit_tp = df.high[k] >= tp if up else df.low[k] <= tp
            if hit_sl:
                exit_px, j = sl, k
                break
            if hit_tp:
                exit_px, j = tp, k
                break
        move = (exit_px - entry) if up else (entry - exit_px)
        r = max(move / s.sl_dist, -1.0) - cost * entry / s.sl_dist
        trades.append({"time": df.time[e], "side": s.side, "r": r, "bars": j - e + 1})
        free_from = j + 1
    return pd.DataFrame(trades, columns=["time", "side", "r", "bars"])


def summarize(t: pd.DataFrame) -> str:
    if t.empty:
        return "no trades"
    eq = t.r.cumsum()
    dd = (eq.cummax() - eq).max()
    return (f"n={len(t):4d} win={100*(t.r>0).mean():4.1f}% exp={t.r.mean():+.3f}R "
            f"total={t.r.sum():+6.1f}R maxDD={dd:4.1f}R")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="frxXAUUSD,frxEURUSD,frxGBPUSD")
    ap.add_argument("--granularity", type=int, default=900)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--cost", type=float, default=0.0002, help="fee as fraction of notional")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    for sym in args.symbols.split(","):
        df = load_candles(sym, args.granularity, args.days, args.refresh)
        mid = len(df) // 2
        print(f"\n{sym}: {len(df)} bars {df.time.iloc[0]:%Y-%m-%d} -> {df.time.iloc[-1]:%Y-%m-%d}")
        for name, fn in STRATEGIES.items():
            sigs = fn(df)
            for label, c in (("zero cost", 0.0), (f"cost {args.cost}", args.cost)):
                t = simulate(df, sigs, c)
                h1, h2 = t[t.time < df.time[mid]], t[t.time >= df.time[mid]]
                print(f"  {name:15s} {label:12s} ALL  {summarize(t)}")
                print(f"  {'':15s} {'':12s} 1st½ {summarize(h1)}\n  {'':15s} {'':12s} 2nd½ {summarize(h2)}")


if __name__ == "__main__":
    main()
