"""
Deriv Over/Under digit-contract trading bot.

Runs exactly one session per invocation (CLI run, or one HTTP call to
server.py's /run endpoint) — it does not schedule itself. An external cron
caller (e.g. a Render Cron Job hitting the deployed URL) decides when to
trigger it.

Session mechanic (by design, not an accident):
- Each session takes flat-stake trades back to back until either (a) one of
  them wins, or (b) MAX_ATTEMPTS is reached — whichever comes first.
- IMPORTANT: this guarantees the session STOPS on the first win, not that it
  is profitable. If it loses several attempts before winning, or never wins
  within MAX_ATTEMPTS, that session's net result can still be negative — no
  staking scheme can change that against a random, house-edged game. See the
  trade_log.csv daily_pnl column for the real outcome.

Safety model (read before running):
- Credentials come from a local .env file next to this script — never hardcode
  a token in code.
- Deriv's auth model is OAuth2 + a per-connection OTP-issued websocket URL, not
  a static token. Visit server.py's /login route once (interactive browser
  login); it saves tokens to deriv_tokens.json, which this script refreshes
  automatically afterward. ACCOUNT ("demo"/"real") picks which account to
  trade against, independently of LIVE_CONFIRM (which picks quote-only vs.
  actually buying) — e.g. account=demo + live_confirm=yes runs the full
  buy/settle loop with play money.
- Defaults to DRY_RUN: fetches one live proposal (real payout quote) but does
  NOT buy, so you can verify connectivity, symbol, barrier, and logging
  before any money moves. Set LIVE_CONFIRM=yes in .env to place real trades.
- Hard circuit breakers stop a session automatically: MAX_DAILY_LOSS (dollar
  cap) and MAX_ATTEMPTS (trade-count cap). These do not create an edge —
  they bound how much a bad day can cost.
- No martingale/progressive staking: every trade uses the same flat STAKE
  from .env, and STAKE must be > 0.
"""

import asyncio
import csv
import itertools
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from websockets.legacy.client import WebSocketClientProtocol, connect
from websockets.exceptions import ConnectionClosed

TOKENS_FILE = Path(__file__).with_name("deriv_tokens.json")
OAUTH_AUTH_URL = "https://auth.deriv.com/oauth2/auth"
OAUTH_TOKEN_URL = "https://auth.deriv.com/oauth2/token"
ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"
OTP_URL_TEMPLATE = "https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


@dataclass
class Config:
    app_id: str
    account: str  # "demo" or "real" — which Deriv account to trade against
    symbol: str
    direction: str  # "OVER" or "UNDER"
    barrier: str  # "0".."9"
    stake: float
    currency: str
    duration: int
    duration_unit: str
    max_daily_loss: float
    max_attempts: int
    live_confirm: bool  # whether to actually buy contracts, vs. quote-only
    log_file: Path

    @classmethod
    def load(cls, overrides: dict | None = None) -> "Config":
        file_env = load_env_file(Path(__file__).with_name(".env"))
        env = {**file_env, **os.environ}  # real process env vars win
        if overrides:
            env = {**env, **{k: v for k, v in overrides.items() if v is not None}}

        def get(key: str, default: str = "", required: bool = False) -> str:
            val = env.get(key, default)
            if required and not val:
                raise SystemExit(f"Missing required config: {key} (set it in .env)")
            return val

        app_id = get("DERIV_APP_ID", required=True)

        account = get("ACCOUNT", "demo").lower()
        if account not in ("demo", "real"):
            raise SystemExit("ACCOUNT must be 'demo' or 'real'")

        direction = get("DIRECTION", "OVER").upper()
        if direction not in ("OVER", "UNDER"):
            raise SystemExit("DIRECTION must be OVER or UNDER")

        barrier = get("BARRIER", "2")
        if barrier not in [str(d) for d in range(10)]:
            raise SystemExit("BARRIER must be a single digit 0-9")

        stake = float(get("STAKE", "1.0"))
        if stake <= 0:
            raise SystemExit("STAKE must be greater than 0")

        return cls(
            app_id=app_id,
            account=account,
            symbol=get("SYMBOL", "R_100"),
            direction=direction,
            barrier=barrier,
            stake=stake,
            currency=get("CURRENCY", "USD"),
            duration=int(get("DURATION", "1")),
            duration_unit=get("DURATION_UNIT", "t"),
            max_daily_loss=float(get("MAX_DAILY_LOSS", required=True)),
            max_attempts=int(get("MAX_ATTEMPTS", "8")),
            live_confirm=get("LIVE_CONFIRM", "no").lower() == "yes",
            log_file=Path(get("LOG_FILE", "trade_log.csv")),
        )


# ---------------------------------------------------------------------------
# OAuth2 token management (see server.py's /login route for the initial interactive login)
# ---------------------------------------------------------------------------


def load_tokens() -> dict:
    if not TOKENS_FILE.exists():
        raise SystemExit(
            "No deriv_tokens.json found. Visit /login?app_id=...&api_key=... once to log in "
            "(see server.py's module docstring)."
        )
    return json.loads(TOKENS_FILE.read_text())


def save_tokens(data: dict) -> None:
    TOKENS_FILE.write_text(json.dumps(data, indent=2))
    TOKENS_FILE.chmod(0o600)


def refresh_access_token(app_id: str, refresh_token: str) -> dict:
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": app_id,
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    if not resp.ok:
        raise SystemExit(
            f"Refreshing the Deriv OAuth token failed ({resp.status_code}): {resp.text}\n"
            "Visit /login?app_id=...&api_key=... again to re-authorize."
        )
    return resp.json()


def ensure_access_token(tokens: dict) -> dict:
    """Refresh in place and persist if the access token is expired or near expiry."""
    if tokens.get("expires_at", 0) <= time.time():
        tok = refresh_access_token(tokens["app_id"], tokens["refresh_token"])
        tokens["access_token"] = tok["access_token"]
        tokens["refresh_token"] = tok.get("refresh_token", tokens["refresh_token"])
        tokens["expires_at"] = time.time() + float(tok.get("expires_in", 600)) - 30
        save_tokens(tokens)
    return tokens


def pick_account(tokens: dict, account_type: str) -> str:
    for acc in tokens.get("accounts", []):
        if acc.get("account_type") == account_type and acc.get("status") == "active":
            return acc["account_id"]
    raise SystemExit(
        f"No active '{account_type}' account found in deriv_tokens.json. "
        "Open one on Deriv, then visit /login?app_id=...&api_key=... again."
    )


# ---------------------------------------------------------------------------
# Deriv WebSocket client
# ---------------------------------------------------------------------------


class DerivApiError(Exception):
    pass


class DerivClient:
    def __init__(self, app_id: str, access_token: str, account_id: str):
        self.app_id = app_id
        self.access_token = access_token
        self.account_id = account_id
        self.ws: WebSocketClientProtocol | None = None
        self._req_id = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._sub_queues: dict[int, asyncio.Queue] = {}
        self._recv_task: asyncio.Task | None = None

    def _fetch_otp_url(self) -> str:
        resp = requests.post(
            OTP_URL_TEMPLATE.format(account_id=self.account_id),
            headers={"Deriv-App-ID": self.app_id, "Authorization": f"Bearer {self.access_token}"},
            timeout=15,
        )
        if not resp.ok:
            raise DerivApiError(f"OTP request failed ({resp.status_code}): {resp.text}")
        return resp.json()["data"]["url"]

    async def connect(self):
        otp_url = await asyncio.to_thread(self._fetch_otp_url)
        self.ws = await connect(otp_url, ping_interval=20, ping_timeout=10)
        self._recv_task = asyncio.create_task(self._receiver())

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
        if self.ws:
            await self.ws.close()

    async def _receiver(self):
        assert self.ws is not None
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                req_id = msg.get("req_id")
                if req_id in self._sub_queues:
                    await self._sub_queues[req_id].put(msg)
                elif req_id in self._pending:
                    fut = self._pending.pop(req_id)
                    if not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except ConnectionClosed:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(DerivApiError("Connection closed"))
            for q in self._sub_queues.values():
                await q.put({"error": {"message": "Connection closed"}})

    async def _request(self, payload: dict, timeout: float = 15.0) -> dict:
        assert self.ws is not None, "call connect() first"
        req_id = next(self._req_id)
        payload = {**payload, "req_id": req_id}
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self.ws.send(json.dumps(payload))
        msg = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in msg:
            raise DerivApiError(msg["error"].get("message", "Unknown API error"))
        return msg

    async def get_balance(self) -> dict:
        msg = await self._request({"balance": 1})
        return msg["balance"]

    async def get_proposal(
        self,
        contract_type: str,
        barrier: str,
        stake: float,
        duration: int,
        duration_unit: str,
        symbol: str,
        currency: str,
    ) -> dict:
        msg = await self._request(
            {
                "proposal": 1,
                "contract_type": contract_type,
                "amount": stake,
                "basis": "stake",
                "currency": currency,
                "duration": duration,
                "duration_unit": duration_unit,
                "underlying_symbol": symbol,
                "barrier": barrier,
            }
        )
        return msg["proposal"]

    async def buy(self, proposal_id: str, price: float) -> dict:
        msg = await self._request({"buy": proposal_id, "price": price})
        return msg["buy"]

    async def wait_for_settlement(self, contract_id: int, timeout: float = 60.0) -> dict:
        assert self.ws is not None, "call connect() first"
        req_id = next(self._req_id)
        queue: asyncio.Queue = asyncio.Queue()
        self._sub_queues[req_id] = queue
        await self.ws.send(
            json.dumps(
                {
                    "proposal_open_contract": 1,
                    "contract_id": contract_id,
                    "subscribe": 1,
                    "req_id": req_id,
                }
            )
        )
        subscription_id = None
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise DerivApiError("Timed out waiting for contract settlement")
                msg = await asyncio.wait_for(queue.get(), timeout=remaining)
                if "error" in msg:
                    raise DerivApiError(msg["error"].get("message", "Unknown API error"))
                subscription_id = msg.get("subscription", {}).get("id", subscription_id)
                poc = msg.get("proposal_open_contract")
                if poc and poc.get("is_sold"):
                    return poc
        finally:
            self._sub_queues.pop(req_id, None)
            if subscription_id:
                try:
                    await self._request({"forget": subscription_id})
                except DerivApiError:
                    pass


# ---------------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------------


@dataclass
class RiskManager:
    """Bounds one daily session: stop on the first win, or on a hard cap.

    Stopping on a win does not make the session profitable by itself — see
    module docstring. `daily_pnl` after the session is the real result.
    """

    max_daily_loss: float
    max_attempts: int
    daily_pnl: float = 0.0
    trades_done: int = 0
    won: bool = False
    stop_reason: str | None = None

    def can_trade(self) -> bool:
        if self.stop_reason:
            return False
        if self.won:
            self.stop_reason = "won a trade — session goal met for today"
        elif self.trades_done >= self.max_attempts:
            self.stop_reason = f"reached max attempts ({self.max_attempts}) without a win"
        elif self.daily_pnl <= -abs(self.max_daily_loss):
            self.stop_reason = f"hit max daily loss ({self.max_daily_loss})"
        return self.stop_reason is None

    def record(self, profit: float):
        self.trades_done += 1
        self.daily_pnl += profit
        if profit > 0:
            self.won = True


# ---------------------------------------------------------------------------
# Trade logging
# ---------------------------------------------------------------------------


class TradeLogger:
    FIELDS = [
        "timestamp",
        "mode",
        "account",
        "symbol",
        "contract_type",
        "barrier",
        "stake",
        "payout",
        "profit",
        "balance_after",
        "contract_id",
    ]

    def __init__(self, path: Path):
        is_new = not path.exists()
        if not is_new:
            self._migrate_if_needed(path)
        self._file = path.open("a", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        if is_new:
            self._writer.writeheader()
            self._file.flush()

    @classmethod
    def _migrate_if_needed(cls, path: Path) -> None:
        """Rewrite the file if its header doesn't match FIELDS (e.g. after an
        older deploy logged rows before a column was added) — appending as-is
        would silently misalign every column after the change."""
        with path.open("r", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames == cls.FIELDS:
                return
            rows = list(reader)
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cls.FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in cls.FIELDS})

    def log(self, **kwargs):
        self._writer.writerow({f: kwargs.get(f, "") for f in self.FIELDS})
        self._file.flush()

    def close(self):
        self._file.close()


# ---------------------------------------------------------------------------
# Trading session
# ---------------------------------------------------------------------------


async def run_session(cfg: Config, contract_type: str, mode: str, logger: TradeLogger) -> dict:
    """Runs exactly one session: DRY_RUN checks one quote; LIVE trades until
    a win, MAX_ATTEMPTS, or MAX_DAILY_LOSS — whichever comes first. `mode`
    controls quote-only vs. actually-buying; `cfg.account` ("demo"/"real")
    controls which account it runs against — the two are independent, so
    LIVE + account=demo runs the full buy/settle loop with play money. Timing
    is the caller's responsibility (e.g. an external cron scheduler hitting
    the API endpoint) — this function does not wait for a daily window
    itself. Returns a JSON-serializable summary of what happened."""
    tokens = ensure_access_token(load_tokens())
    account_id = pick_account(tokens, cfg.account)
    client = DerivClient(cfg.app_id, tokens["access_token"], account_id)
    summary: dict = {"mode": mode, "account": cfg.account, "symbol": cfg.symbol, "contract_type": contract_type}
    try:
        await client.connect()
        bal = await client.get_balance()
        summary["loginid"] = bal["loginid"]
        summary["balance"] = bal["balance"]
        print(f"Connected to {bal['loginid']} | balance: {bal['balance']} {bal['currency']}")

        if mode == "DRY_RUN":
            try:
                proposal = await client.get_proposal(
                    contract_type, cfg.barrier, cfg.stake, cfg.duration,
                    cfg.duration_unit, cfg.symbol, cfg.currency,
                )
                ask_price = float(proposal["ask_price"])
                payout = float(proposal["payout"])
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                print(
                    f"[DRY_RUN] {contract_type} barrier={cfg.barrier} stake={ask_price:.2f} "
                    f"payout={payout:.2f} — quote only, no purchase made"
                )
                logger.log(
                    timestamp=ts, mode=mode, account=cfg.account, symbol=cfg.symbol,
                    contract_type=contract_type, barrier=cfg.barrier, stake=ask_price, payout=payout,
                )
                summary.update(status="quoted", ask_price=ask_price, payout=payout)
            except DerivApiError as e:
                summary.update(status="error", error=str(e))
            return summary

        risk = RiskManager(cfg.max_daily_loss, cfg.max_attempts)
        while risk.can_trade():
            try:
                proposal = await client.get_proposal(
                    contract_type, cfg.barrier, cfg.stake, cfg.duration,
                    cfg.duration_unit, cfg.symbol, cfg.currency,
                )
            except DerivApiError as e:
                print(f"Proposal error: {e}")
                await asyncio.sleep(2)
                continue

            ask_price = float(proposal["ask_price"])
            payout = float(proposal["payout"])
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(
                f"[{ts}] attempt {risk.trades_done + 1}/{cfg.max_attempts} {contract_type} "
                f"barrier={cfg.barrier} stake={ask_price:.2f} payout={payout:.2f}"
            )

            try:
                bought = await client.buy(proposal["id"], ask_price)
                settled = await client.wait_for_settlement(bought["contract_id"])
            except DerivApiError as e:
                print(f"Trade error: {e}")
                await asyncio.sleep(2)
                continue

            profit = float(settled.get("profit", 0))
            risk.record(profit)

            try:
                balance = await client.get_balance()
                balance_after = balance["balance"]
            except DerivApiError:
                balance_after = ""

            logger.log(
                timestamp=ts, mode=mode, account=cfg.account, symbol=cfg.symbol,
                contract_type=contract_type, barrier=cfg.barrier, stake=ask_price, payout=payout,
                profit=profit, balance_after=balance_after, contract_id=bought.get("contract_id"),
            )

            result = "WON" if profit > 0 else "LOST"
            print(
                f"  -> {result} profit={profit:+.2f} | daily_pnl={risk.daily_pnl:+.2f} "
                f"| attempts={risk.trades_done}/{cfg.max_attempts}"
            )

            await asyncio.sleep(1)

        if risk.won:
            print(f"Session done: WON on attempt {risk.trades_done} | net daily_pnl={risk.daily_pnl:+.2f}")
        else:
            print(
                f"Session done WITHOUT a win ({risk.stop_reason}) | "
                f"net daily_pnl={risk.daily_pnl:+.2f} — today is a net loss despite the cap"
            )
        summary.update(
            status="won" if risk.won else "stopped",
            stop_reason=risk.stop_reason,
            trades_done=risk.trades_done,
            daily_pnl=risk.daily_pnl,
        )
        return summary
    finally:
        await client.close()


async def run():
    """CLI entrypoint: runs a single session using .env config, then exits.
    For scheduled/repeated runs, use server.py behind an external cron caller."""
    cfg = Config.load()
    mode = "LIVE" if cfg.live_confirm else "DRY_RUN"
    contract_type = "DIGITOVER" if cfg.direction == "OVER" else "DIGITUNDER"
    logger = TradeLogger(cfg.log_file)
    print(f"=== Deriv Over/Under bot | {mode} | account={cfg.account} stake={cfg.stake} app_id={cfg.app_id} ===")
    try:
        summary = await run_session(cfg, contract_type, mode, logger)
        print(summary)
    finally:
        logger.close()


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        raise SystemExit("Requires Python 3.10+")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
