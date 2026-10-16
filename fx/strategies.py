"""Entry signals on closed candles. Each strategy returns a list of Signal
objects (one per qualifying bar); the entry is assumed at the NEXT bar's open.

sl_dist / tp_dist are price distances, so sizing and backtesting are
independent of the instrument's price scale."""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Signal:
    idx: int  # bar index whose close produced the signal
    side: str  # "UP" or "DOWN"
    sl_dist: float
    tp_dist: float


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def trend_pullback(df, ema_trend=200, ema_pull=20, atr_mult=1.5, rr=2.0, start_hour=7, end_hour=17):
    """With the 200-EMA trend, buy a dip that tags the 20-EMA and closes back
    above it on a bullish candle (mirror for shorts). Only during the
    London/New York session (UTC hours)."""
    et = df.close.ewm(span=ema_trend, adjust=False).mean()
    ep = df.close.ewm(span=ema_pull, adjust=False).mean()
    a = atr(df)
    hour = df.time.dt.hour
    out = []
    for i in range(ema_trend, len(df) - 1):
        if not (start_hour <= hour[i] < end_hour) or np.isnan(a[i]):
            continue
        c, o, lo, hi = df.close[i], df.open[i], df.low[i], df.high[i]
        sl = atr_mult * a[i]
        if c > et[i] and lo <= ep[i] < c and c > o:
            out.append(Signal(i, "UP", sl, sl * rr))
        elif c < et[i] and hi >= ep[i] > c and c < o:
            out.append(Signal(i, "DOWN", sl, sl * rr))
    return out


def asian_breakout(df, asia_end=7, trade_end=12, rr=1.5, min_atr=1.5, max_atr=8.0, max_sl_atr=4.0):
    """Mark the 00:00-07:00 UTC range; take the first close beyond it before
    `trade_end`, stop at the other side of the range (capped), once per day.
    Ranges that are too tight or too wide (in ATR units) are skipped."""
    a = atr(df)
    out = []
    day = df.time.dt.date
    hour = df.time.dt.hour
    for d, g in df.groupby(day):
        asia = g[hour[g.index] < asia_end]
        if len(asia) < 10:
            continue
        hi, lo = asia.high.max(), asia.low.min()
        for i in g.index[hour[g.index] >= asia_end]:
            if hour[i] >= trade_end or np.isnan(a[i]):
                break
            width = (hi - lo) / a[i]
            if not (min_atr <= width <= max_atr):
                break
            c = df.close[i]
            if c > hi:
                sl = min(c - lo, max_sl_atr * a[i])
                out.append(Signal(i, "UP", sl, sl * rr))
                break
            if c < lo:
                sl = min(hi - c, max_sl_atr * a[i])
                out.append(Signal(i, "DOWN", sl, sl * rr))
                break
    return out


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def scalp_pullback(df, rr=0.8, ema_trend=100, rsi_n=3, lo=25, hi=75, sl_atr=1.5, start_hour=0, end_hour=24):
    """Frequent short-horizon scalp: buy a sharp RSI dip inside an up-trend
    (close above the trend EMA), sell a sharp spike inside a down-trend. Small
    target (rr < 1 x the stop) so most trades close for a little profit; meant
    for M1 bars with a time stop."""
    et = df.close.ewm(span=ema_trend, adjust=False).mean().to_numpy()
    r = rsi(df.close, rsi_n).to_numpy()
    a = atr(df).to_numpy()
    c = df.close.to_numpy()
    hour = df.time.dt.hour.to_numpy()
    out = []
    for i in range(ema_trend, len(df)):
        if not (start_hour <= hour[i] < end_hour) or np.isnan(a[i]) or np.isnan(r[i]):
            continue
        sl = sl_atr * a[i]
        if r[i] < lo and c[i] > et[i]:
            out.append(Signal(i, "UP", sl, sl * rr))
        elif r[i] > hi and c[i] < et[i]:
            out.append(Signal(i, "DOWN", sl, sl * rr))
    return out


# once_per_day: the strategy fires a single setup a day, so the bot must not re-enter.
STRATEGIES = {"trend_pullback": trend_pullback, "asian_breakout": asian_breakout, "scalp_pullback": scalp_pullback}
ONCE_PER_DAY = {"asian_breakout"}
