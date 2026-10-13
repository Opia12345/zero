"""Offline tests for the trading loop: fake Deriv client, Telegram stubbed, no network."""

import asyncio
import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot_script as b


class FakeClient:
    """Even/odd quote at RTP 0.97; every other contract is rejected."""

    buys: list = []
    profit = -0.35

    def __init__(self, *a):
        pass

    async def connect(self):
        pass

    async def close(self):
        pass

    async def get_balance(self):
        return {"loginid": "VRTC1", "balance": 100, "currency": "USD"}

    async def get_proposal(self, contract_type, barrier, stake, *a):
        if contract_type in ("DIGITEVEN", "DIGITODD"):
            return {"ask_price": stake, "payout": round(stake * 0.97 / 0.5, 2)}
        raise b.DerivApiError("not offered")

    async def buy_now(self, contract_type, barrier, stake, *a):
        FakeClient.buys.append((contract_type, barrier))
        return {"contract_id": 1, "buy_price": stake, "payout": stake * 1.94}

    async def wait_for_settlement(self, contract_id):
        return {"profit": FakeClient.profit}


def run_bot(live: bool, extra: dict | None = None, seconds: float = 8) -> tuple[list[dict], list[str]]:
    tmp = Path(tempfile.mkdtemp())
    sent: list[str] = []

    def fake_telegram(cfg, text):
        sent.append(text)

    overrides = {
        "MAX_DAILY_LOSS": "0.5",
        "LOG_FILE": str(tmp / "t.csv"),
        "LIVE_CONFIRM": "yes" if live else "no",
        "SYMBOLS": "R_10",
        "TRADE_COOLDOWN": "0",
        "SCAN_INTERVAL": "1",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        **(extra or {}),
    }
    FakeClient.buys = []
    with mock.patch.object(b, "DerivClient", FakeClient), \
         mock.patch.object(b, "load_tokens", lambda: {}), \
         mock.patch.object(b, "ensure_access_token", lambda t: {"access_token": "x", "app_id": "1"}), \
         mock.patch.object(b, "pick_account", lambda t, a: "A"), \
         mock.patch.object(b, "send_telegram_message", fake_telegram):
        cfg = b.Config.load(overrides)
        logger = b.TradeLogger(cfg.log_file)

        async def go():
            try:
                await asyncio.wait_for(b.watch_and_trade(cfg, logger), timeout=seconds)
            except asyncio.TimeoutError:
                pass

        try:
            asyncio.run(go())
        finally:
            logger.close()
    with cfg.log_file.open(newline="") as f:
        return list(csv.DictReader(f)), sent


class BotTests(unittest.TestCase):
    def test_dry_run_logs_quote_and_never_buys(self):
        rows, sent = run_bot(live=False, seconds=6)
        self.assertTrue(rows)
        self.assertEqual(FakeClient.buys, [])
        self.assertEqual(rows[0]["mode"], "DRY_RUN")
        self.assertEqual(rows[0]["contract_type"], "DIGITEVEN")
        self.assertEqual(rows[0]["barrier"], "")

    def test_live_buys_best_quote_without_barrier_and_stops_at_loss_cap(self):
        FakeClient.profit = -0.35
        rows, sent = run_bot(live=True, seconds=20)
        live = [r for r in rows if r["mode"] == "LIVE"]
        # cap is 0.5 and each loss is 0.35: second loss reaches it, third must not happen
        self.assertEqual(len(live), 2)
        self.assertTrue(all(c == ("DIGITEVEN", None) for c in FakeClient.buys))
        self.assertTrue(any("paused" in m for m in sent))

    def test_min_rtp_floor_blocks_trading(self):
        rows, _ = run_bot(live=True, extra={"MIN_RTP": "0.99"}, seconds=6)
        self.assertEqual(FakeClient.buys, [])
        self.assertEqual(rows, [])

    def test_telegram_does_not_block_event_loop(self):
        import time

        def slow(cfg, text):
            time.sleep(1)

        async def go():
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.05)
                    ticks += 1

            t = asyncio.create_task(ticker())
            with mock.patch.object(b, "send_telegram_message", slow):
                await b.notify(None, "x")
            t.cancel()
            return ticks

        self.assertGreater(asyncio.run(go()), 10)


if __name__ == "__main__":
    unittest.main()
