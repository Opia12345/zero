import pandas as pd

from fx.backtest import simulate
from fx.strategies import Signal
from fx_bot import plan_trade

MULTS = [100, 200, 300, 500, 800]


def test_plan_trade_risk_matches_stop():
    m, stake, sl, tp = plan_trade(4090.0, 12.0, 1.0, 1.5, MULTS, 20)
    assert sl < stake and abs(sl - 1.0) < 0.02 and tp == round(sl * 1.5, 2)
    assert abs(stake * m * 12.0 / 4090.0 - sl) < 0.02  # loss at the stop price == stop_loss


def test_plan_trade_skips_when_stop_too_wide():
    assert plan_trade(1.1, 0.05, 1.0, 1.5, MULTS, 20) is None  # 4.5% stop needs a <100x multiplier


def _df(rows):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["time"] = pd.date_range("2026-01-01", periods=len(df), freq="15min", tz="UTC")
    return df


def test_simulate_stop_first_when_bar_touches_both():
    df = _df([(100, 100, 100, 100), (100, 103, 97, 100), (100, 100, 100, 100)])
    t = simulate(df, [Signal(0, "UP", 2.0, 2.0)], cost=0)
    assert t.r.iloc[0] == -1.0


def test_simulate_target_and_cost():
    df = _df([(100, 100, 100, 100), (100, 103, 99.5, 102), (100, 100, 100, 100)])
    t = simulate(df, [Signal(0, "UP", 2.0, 3.0)], cost=0.001)
    assert abs(t.r.iloc[0] - (1.5 - 0.001 * 100 / 2.0)) < 1e-9
