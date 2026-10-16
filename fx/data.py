"""Historical candles from Deriv's public websocket (no login needed), cached
as CSV under fx/data/ so backtests don't re-download."""

import asyncio
import json
import time
from pathlib import Path

import pandas as pd
from websockets.legacy.client import connect

PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"
CACHE_DIR = Path(__file__).with_name("data")
COLUMNS = ["epoch", "open", "high", "low", "close"]


async def _page(symbol: str, granularity: int, end, tries: int = 10) -> list[dict]:
    """One page of candles on a fresh connection; the public socket sometimes
    stalls on a request, so time out and retry rather than hang."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            async with connect(PUBLIC_WS, ping_interval=None) as ws:
                await ws.send(json.dumps({
                    "ticks_history": symbol, "style": "candles", "granularity": granularity,
                    "count": 1000, "end": end, "req_id": 1,
                }))
                msg = json.loads(await asyncio.wait_for(ws.recv(), 45))
            if "error" in msg:
                raise RuntimeError(f"{symbol}: {msg['error'].get('message')}")
            return msg.get("candles", [])
        except (asyncio.TimeoutError, OSError, ConnectionError) as e:
            last = e
    raise RuntimeError(f"{symbol}: candle request kept failing ({last!r})")


async def _fetch(symbol: str, granularity: int, days: int) -> pd.DataFrame:
    rows: dict[int, dict] = {}
    cutoff = int(time.time()) - days * 86400
    end: int | str = "latest"
    while True:
        candles = await _page(symbol, granularity, end)
        if not candles:
            break
        before = len(rows)
        for c in candles:
            rows[c["epoch"]] = c
        oldest = min(c["epoch"] for c in candles)
        if oldest <= cutoff or len(rows) == before:
            break
        end = oldest - 1
    df = pd.DataFrame(sorted(rows.values(), key=lambda c: c["epoch"]))[COLUMNS]
    return df[df.epoch >= cutoff].reset_index(drop=True)


def load_candles(symbol: str, granularity: int, days: int = 180, refresh: bool = False) -> pd.DataFrame:
    """Returns a DataFrame [epoch, open, high, low, close, time(UTC)]."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{symbol}_{granularity}.csv"
    if refresh or not path.exists() or time.time() - path.stat().st_mtime > 6 * 3600:
        df = asyncio.run(_fetch(symbol, granularity, days))
        df.to_csv(path, index=False)
    else:
        df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df.epoch, unit="s", utc=True)
    return df
