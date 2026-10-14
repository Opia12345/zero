"""Forex/gold bot: Asian-range breakout on Deriv multipliers.

    python3 fx_bot.py            # DRY_RUN: asks Deriv for a real quote, buys nothing
    LIVE_CONFIRM=yes python3 fx_bot.py

On Render, server.py runs run_forever() as a background task instead.

Settings come from .env locally or environment variables on Render:

    DERIV_APP_ID, ACCOUNT=demo|real, LIVE_CONFIRM=yes|no (default no = quotes only)
    MAX_DAILY_LOSS (required), MAX_ATTEMPTS=4, CURRENCY=USD
    FX_SYMBOLS=frxXAUUSD        comma list, e.g. frxXAUUSD,frxEURUSD
    FX_RISK=1.0                 dollars lost if the stop-loss is hit (1R)
    FX_RR=1.5                   take-profit = RR x stop-loss
    FX_MAX_STAKE=20             refuse trades needing a bigger stake
    FX_LOG_FILE=fx_trade_log.csv, TOKENS_FILE=deriv_tokens.json (put both on a
    persistent disk on Render so restarts keep the daily limits and the login)
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional)

Every trade carries a server-side stop-loss and take-profit, so an open
position stays protected if this process dies. Positions are closed at market
after 24h (the same limit the backtest assumes). MAX_DAILY_LOSS and
MAX_ATTEMPTS cap each UTC day; one trade per symbol per day.

The backtest (fx/backtest.py) is evidence the idea is not obviously broken,
not proof of an edge. Run it on a demo account first and read the commission
that DRY_RUN prints before trusting any backtest number.
"""

import asyncio
import csv
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from websockets.exceptions import ConnectionClosed

import deriv_core as b
from fx.backtest import MAX_HOLD
from fx.data import _page
from fx.strategies import asian_breakout

GRANULARITY = 900
LOG_FIELDS = ["timestamp", "mode", "account", "symbol", "side", "multiplier", "stake", "stop_loss",
              "take_profit", "commission", "entry_price", "profit", "contract_id"]


@dataclass
class FxConfig:
    app_id: str
    account: str
    live_confirm: bool
    currency: str
    max_daily_loss: float
    max_attempts: int
    symbols: list[str]
    risk: float
    rr: float
    max_stake: float
    log_file: Path
    telegram_bot_token: str
    telegram_chat_id: str

    @classmethod
    def load(cls) -> "FxConfig":
        def need(key: str) -> str:
            val = b.env_get(key)
            if not val:
                raise SystemExit(f"Missing required config: {key}")
            return val

        account = b.env_get("ACCOUNT", "demo").lower()
        if account not in ("demo", "real"):
            raise SystemExit("ACCOUNT must be 'demo' or 'real'")
        cfg = cls(
            app_id=need("DERIV_APP_ID"),
            account=account,
            live_confirm=b.env_get("LIVE_CONFIRM", "no").lower() == "yes",
            currency=b.env_get("CURRENCY", "USD"),
            max_daily_loss=float(need("MAX_DAILY_LOSS")),
            max_attempts=int(b.env_get("MAX_ATTEMPTS", "4")),
            symbols=[x.strip() for x in b.env_get("FX_SYMBOLS", "frxXAUUSD").split(",") if x.strip()],
            risk=float(b.env_get("FX_RISK", "1.0")),
            rr=float(b.env_get("FX_RR", "1.5")),
            max_stake=float(b.env_get("FX_MAX_STAKE", "20")),
            log_file=Path(b.env_get("FX_LOG_FILE", "fx_trade_log.csv")),
            telegram_bot_token=b.env_get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=b.env_get("TELEGRAM_CHAT_ID"),
        )
        if cfg.risk <= 0 or cfg.rr <= 0 or cfg.max_daily_loss <= 0:
            raise SystemExit("FX_RISK, FX_RR and MAX_DAILY_LOSS must be positive")
        return cfg


def plan_trade(price: float, sl_dist: float, risk: float, rr: float, multipliers: list[int], max_stake: float):
    """Picks (multiplier, stake, stop_loss$, take_profit$) so that a stop at
    sl_dist costs `risk` dollars. Multiplier P&L is stake*mult*move/price, so
    stake = risk*price/(mult*sl_dist). Deriv needs stop_loss < stake (the
    stop-out level), so use the largest multiplier that leaves 10% headroom.
    Returns None if no multiplier fits."""
    for m in sorted(multipliers, reverse=True):
        stake = round(risk * price / (m * sl_dist), 2)
        if stake < risk * 1.1 or stake > max_stake:
            continue
        stop_loss = round(stake * m * sl_dist / price, 2)
        if stop_loss >= stake:
            continue
        return m, stake, stop_loss, round(stop_loss * rr, 2)
    return None


class FxClient(b.DerivClient):
    async def multiplier_range(self, symbol: str) -> list[int]:
        msg = await self._request({"contracts_for": symbol})
        for c in msg["contracts_for"]["available"]:
            if c["contract_type"] == "MULTUP":
                return list(c["multiplier_range"])
        raise b.DerivApiError(f"{symbol} has no multiplier contracts")

    @staticmethod
    def _params(side, stake, mult, symbol, currency, stop_loss, take_profit):
        return {
            "contract_type": "MULTUP" if side == "UP" else "MULTDOWN",
            "amount": stake, "basis": "stake", "currency": currency,
            "multiplier": mult, "underlying_symbol": symbol,
            "limit_order": {"stop_loss": stop_loss, "take_profit": take_profit},
        }

    async def quote(self, *args) -> dict:
        msg = await self._request({"proposal": 1, **self._params(*args)})
        return msg["proposal"]

    async def open(self, *args) -> dict:
        stake = args[1]
        msg = await self._request({"buy": 1, "price": stake, "parameters": self._params(*args)})
        return msg["buy"]

    async def close_now(self, contract_id: int) -> None:
        await self._request({"sell": contract_id, "price": 0})


class FxLog:
    def __init__(self, path: Path):
        new = not path.exists()
        self.path = path
        self._f = path.open("a", newline="")
        self._w = csv.DictWriter(self._f, fieldnames=LOG_FIELDS)
        if new:
            self._w.writeheader()

    def log(self, **kw):
        self._w.writerow({k: kw.get(k, "") for k in LOG_FIELDS})
        self._f.flush()


def todays_history(path: Path) -> tuple[set, float, int]:
    """(symbols already traded today, today's P&L, trade count) from the log."""
    traded, pnl, n = set(), 0.0, 0
    if not path.exists():
        return traded, pnl, n
    today = datetime.now(timezone.utc).date().isoformat()
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row["timestamp"].startswith(today) and row["mode"] == "LIVE":
                traded.add(row["symbol"])
                n += 1
                pnl += float(row["profit"] or 0)
    return traded, pnl, n


async def latest_candles(symbol: str) -> pd.DataFrame:
    """Closed M15 candles for the last ~2 days (enough for the Asian range + ATR)."""
    candles = await _page(symbol, GRANULARITY, "latest")
    df = pd.DataFrame(candles)[["epoch", "open", "high", "low", "close"]]
    df = df[df.epoch + GRANULARITY <= time.time()].reset_index(drop=True)  # drop the forming bar
    df["time"] = pd.to_datetime(df.epoch, unit="s", utc=True)
    return df


async def manage_position(client, cfg, log, risk, symbol, side, bought, plan, commission, entry_ts):
    cid = bought["contract_id"]
    try:
        try:
            poc = await client.wait_for_settlement(cid, timeout=MAX_HOLD * GRANULARITY)
        except b.DerivApiError as e:
            if "Timed out" not in str(e):
                raise
            print(f"{symbol}: {MAX_HOLD}-bar time limit hit, closing at market")
            await client.close_now(cid)
            poc = await client.wait_for_settlement(cid, timeout=60)
        profit = float(poc.get("profit", 0))
    except (b.DerivApiError, ConnectionClosed) as e:
        print(f"{symbol}: lost track of contract {cid} ({e}); its stop-loss/take-profit still apply on Deriv's side")
        return
    risk.record(profit)
    mult, stake, sl, tp = plan
    log.log(timestamp=entry_ts, mode="LIVE", account=cfg.account, symbol=symbol, side=side, multiplier=mult,
            stake=stake, stop_loss=sl, take_profit=tp, commission=commission,
            entry_price=poc.get("entry_spot", ""), profit=profit, contract_id=cid)
    print(f"{symbol} {side} settled {profit:+.2f} | day pnl {risk.daily_pnl:+.2f}")
    await b.notify(cfg, f"{'✅' if profit > 0 else '❌'} {symbol} {side} x{mult}: {profit:+.2f} {cfg.currency}\n"
                        f"Day total {risk.daily_pnl:+.2f} ({risk.trades_done}/{cfg.max_attempts} trades)")


async def run(cfg: FxConfig):
    symbols, risk_usd, rr, max_stake = cfg.symbols, cfg.risk, cfg.rr, cfg.max_stake
    log_path = cfg.log_file
    log = FxLog(log_path)
    live = cfg.live_confirm
    mode = "LIVE" if live else "DRY_RUN"

    tokens = b.ensure_access_token(b.load_tokens())
    client = FxClient(cfg.app_id, tokens["access_token"], b.pick_account(tokens, cfg.account))
    await client.connect()
    bal = await client.get_balance()
    print(f"Connected to {bal['loginid']} ({cfg.account}) balance {bal['balance']} {bal['currency']} | "
          f"{mode} | {symbols} | risk ${risk_usd}/trade RR {rr}")

    mults = {s: await client.multiplier_range(s) for s in symbols}
    traded, pnl, n = todays_history(log_path)
    day = datetime.now(timezone.utc).date()
    risk = b.RiskManager(cfg.max_daily_loss, cfg.max_attempts, stop_on_win=False, daily_pnl=pnl, trades_done=n)
    tasks: set[asyncio.Task] = set()
    quoted_today: set[str] = set()

    try:
        while True:
            # wake a few seconds after each M15 bar closes
            await asyncio.sleep(GRANULARITY - (time.time() % GRANULARITY) + 5)
            if datetime.now(timezone.utc).date() != day:
                day = datetime.now(timezone.utc).date()
                traded, quoted_today = set(), set()
                risk = b.RiskManager(cfg.max_daily_loss, cfg.max_attempts, stop_on_win=False)
            if not risk.can_trade():
                print(f"Idle: {risk.stop_reason}")
                continue
            for sym in symbols:
                if sym in traded or sym in quoted_today:
                    continue
                try:
                    df = await latest_candles(sym)
                except RuntimeError as e:
                    print(f"{sym}: {e}")
                    continue
                sigs = [s for s in asian_breakout(df, rr=rr) if s.idx == len(df) - 1]
                if not sigs:
                    continue
                sig, price = sigs[0], float(df.close.iloc[-1])
                plan = plan_trade(price, sig.sl_dist, risk_usd, rr, mults[sym], max_stake)
                if plan is None:
                    print(f"{sym}: {sig.side} signal skipped, stop {sig.sl_dist:.4f} fits no multiplier")
                    continue
                mult, stake, sl, tp = plan
                args = (sig.side, stake, mult, sym, cfg.currency, sl, tp)
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                quote = await client.quote(*args)
                commission = quote.get("commission", "")
                print(f"[{ts}] {sym} {sig.side} x{mult} stake {stake} SL ${sl} TP ${tp} | "
                      f"ask {quote.get('ask_price')} commission {commission}")
                if not live:
                    quoted_today.add(sym)
                    log.log(timestamp=ts, mode="DRY_RUN", account=cfg.account, symbol=sym, side=sig.side,
                            multiplier=mult, stake=stake, stop_loss=sl, take_profit=tp, commission=commission)
                    continue
                bought = await client.open(*args)
                traded.add(sym)
                t = asyncio.create_task(manage_position(client, cfg, log, risk, sym, sig.side, bought,
                                                        (mult, stake, sl, tp), commission, ts))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
                await b.notify(cfg, f"📈 Opened {sym} {sig.side} x{mult}, stake {stake}, SL ${sl}, TP ${tp}")
                if not risk.can_trade():
                    break
    finally:
        for t in tasks:
            t.cancel()
        await client.close()


async def run_forever(cfg: FxConfig):
    """Reconnects with backoff when the websocket or network drops."""
    delay = 5
    while True:
        started = time.time()
        try:
            await run(cfg)
        except (b.DerivApiError, ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            if time.time() - started > 600:
                delay = 5  # it ran fine for a while, so start the backoff over
            print(f"Disconnected ({e!r}); reconnecting in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


def main():
    try:
        asyncio.run(run_forever(FxConfig.load()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
