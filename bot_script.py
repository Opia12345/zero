"""
Deriv digit-trigger trading bot.

Default mode (CLI: `python bot_script.py`) runs forever as a local, always-on
process: it stays connected, watches every live tick on SYMBOL, and only
places a trade the instant a tick's last digit equals DIGIT_TRIGGER (default
"0"). The trade placed is DIGITDIFF barrier=DIGIT_TRIGGER — i.e. "bet the
NEXT digit differs from the trigger digit". Just start it and leave the
terminal open (or run it under `caffeinate -i python bot_script.py` on macOS
so the trade loop keeps running even if the display sleeps); it keeps trading
for as long as your machine is on and connected. Stop it with Ctrl+C.

`python bot_script.py --once` keeps the old behavior instead: a single blind
OVER/UNDER session using DIRECTION/BARRIER from .env, then exit. That's what
server.py's /run HTTP endpoint calls too (for an external cron/Render setup).

Session/day mechanic (by design, not an accident):
- Within a calendar day (UTC), the bot fires a flat-stake trigger trade on
  EVERY tick that matches DIGIT_TRIGGER — through any number of wins — until
  the FIRST losing trade, or MAX_ATTEMPTS, or MAX_DAILY_LOSS, whichever
  comes first. It does not stop after a win; it stops after exactly one
  loss, regardless of that loss's size. Once stopped, it stops TRADING for
  the rest of that day but keeps watching ticks, and automatically resumes
  trading right after the date rolls over.
- IMPORTANT: trading all day does not guarantee the day is profitable — no
  staking scheme can change that against a random, house-edged game. See the
  trade_log.csv daily_pnl column for the real outcome. MAX_DAILY_LOSS and
  MAX_ATTEMPTS remain as backstops behind the stop-on-first-loss rule, but
  size STAKE with the stop-on-first-loss rule in mind: the single loss that
  ends the day can be as large as one STAKE.

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
- Defaults to DRY_RUN: on every trigger digit, fetches one live proposal
  (real payout quote) but does NOT buy, so you can verify connectivity,
  symbol, and logging before any money moves. Set LIVE_CONFIRM=yes in .env to
  place real trades.
- Hard circuit breakers stop trading automatically: MAX_DAILY_LOSS (dollar
  cap) and MAX_ATTEMPTS (trade-count cap). These do not create an edge —
  they bound how much a bad day can cost.
- No martingale/progressive staking: every trade uses the same flat STAKE
  from .env, and STAKE must be > 0.
- If the websocket drops (network blip, laptop sleep/wake), the bot
  reconnects automatically with backoff — it does not need to be restarted
  by hand.
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
    direction: str  # "OVER" or "UNDER" — used only by --once (run_once)
    barrier: str  # "0".."9" — used only by --once (run_once)
    digit_trigger: str  # "0".."9" — watched digit for the default watch_and_trade mode
    stake: float
    currency: str
    duration: int
    duration_unit: str
    max_daily_loss: float
    max_attempts: int
    live_confirm: bool  # whether to actually buy contracts, vs. quote-only
    log_file: Path
    telegram_bot_token: str
    telegram_chat_id: str

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

        digit_trigger = get("DIGIT_TRIGGER", "0")
        if digit_trigger not in [str(d) for d in range(10)]:
            raise SystemExit("DIGIT_TRIGGER must be a single digit 0-9")

        stake = float(get("STAKE", "1.0"))
        if stake <= 0:
            raise SystemExit("STAKE must be greater than 0")

        return cls(
            app_id=app_id,
            account=account,
            symbol=get("SYMBOL", "R_100"),
            direction=direction,
            barrier=barrier,
            digit_trigger=digit_trigger,
            stake=stake,
            currency=get("CURRENCY", "USD"),
            duration=int(get("DURATION", "1")),
            duration_unit=get("DURATION_UNIT", "t"),
            max_daily_loss=float(get("MAX_DAILY_LOSS", required=True)),
            max_attempts=int(get("MAX_ATTEMPTS", "8")),
            live_confirm=get("LIVE_CONFIRM", "no").lower() == "yes",
            log_file=Path(get("LOG_FILE", "trade_log.csv")),
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=get("TELEGRAM_CHAT_ID", ""),
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

    async def subscribe_ticks(self, symbol: str) -> tuple[int, "asyncio.Queue"]:
        """Starts a live tick subscription; caller reads messages off the
        returned queue (each one a raw {"tick": {...}} or {"error": {...}}
        dict) for as long as the connection stays open."""
        assert self.ws is not None, "call connect() first"
        req_id = next(self._req_id)
        queue: asyncio.Queue = asyncio.Queue()
        self._sub_queues[req_id] = queue
        await self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": req_id}))
        return req_id, queue

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
    """Bounds trading for one calendar day. MAX_DAILY_LOSS and MAX_ATTEMPTS
    are hard caps: trading stops for the day once either is hit — that does
    not make the day profitable by itself, see module docstring. `daily_pnl`
    after the day is the real result.

    `stop_on_win` additionally stops trading after the very first winning
    trade. This is the legacy single-shot (--once / server.py's /run)
    session model: "stop as soon as you're up, don't try to grind out more."

    `stop_on_loss` stops trading after the very first LOSING trade,
    regardless of its size or the cumulative daily_pnl — a single loss ends
    the day, not a dollar threshold. This is distinct from MAX_DAILY_LOSS,
    which only stops trading once cumulative losses cross a dollar amount;
    with stop_on_loss=True, MAX_DAILY_LOSS still applies too, but as a
    backstop that (given the flag) will essentially never be the reason
    trading stops.

    The continuous watch_and_trade loop sets stop_on_win=False and
    stop_on_loss=True: it keeps firing on every trigger digit all day
    through any number of wins, and stops for the day the moment one trade
    loses.
    """

    max_daily_loss: float
    max_attempts: int
    stop_on_win: bool = True
    stop_on_loss: bool = False
    daily_pnl: float = 0.0
    trades_done: int = 0
    won: bool = False
    lost: bool = False
    stop_reason: str | None = None

    def can_trade(self) -> bool:
        if self.stop_reason:
            return False
        if self.stop_on_win and self.won:
            self.stop_reason = "won a trade — session goal met for today"
        elif self.stop_on_loss and self.lost:
            self.stop_reason = "lost a trade — stopping for today"
        elif self.trades_done >= self.max_attempts:
            self.stop_reason = f"reached max attempts ({self.max_attempts})"
        elif self.daily_pnl <= -abs(self.max_daily_loss):
            self.stop_reason = f"hit max daily loss ({self.max_daily_loss})"
        return self.stop_reason is None

    def record(self, profit: float):
        self.trades_done += 1
        self.daily_pnl += profit
        if profit > 0:
            self.won = True
        else:
            self.lost = True


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------


def describe_contract(contract_type: str, barrier: str) -> str:
    if contract_type == "DIGITDIFF":
        return f"differs from {barrier}"
    return f"{'over' if contract_type == 'DIGITOVER' else 'under'} {barrier}"


def send_telegram_message(cfg: Config, text: str) -> None:
    """Best-effort notification — a Telegram outage must never take down a
    trading session, so failures are logged and swallowed, not raised."""
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": text},
            timeout=10,
        )
        if not resp.ok:
            print(f"Telegram notify failed ({resp.status_code}): {resp.text}")
    except requests.RequestException as e:
        print(f"Telegram notify failed: {e}")


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
            trade_desc = describe_contract(contract_type, cfg.barrier)
            if profit > 0:
                headline = f"✅ Trade won: +{profit:.2f} {cfg.currency}"
            else:
                headline = f"❌ Trade lost: {profit:.2f} {cfg.currency}"
            send_telegram_message(
                cfg,
                f"{headline}\n"
                f"{cfg.symbol}, {trade_desc}, {cfg.account} account\n"
                f"📊 Attempt {risk.trades_done} of {cfg.max_attempts} today — running total: "
                f"{risk.daily_pnl:+.2f} {cfg.currency}",
            )

            await asyncio.sleep(1)

        if risk.won:
            print(f"Session done: WON on attempt {risk.trades_done} | net daily_pnl={risk.daily_pnl:+.2f}")
            send_telegram_message(
                cfg,
                f"🏁 Session over — won on attempt {risk.trades_done} of {cfg.max_attempts}.\n"
                f"💰 Net result today: {risk.daily_pnl:+.2f} {cfg.currency}",
            )
        else:
            print(
                f"Session done WITHOUT a win ({risk.stop_reason}) | "
                f"net daily_pnl={risk.daily_pnl:+.2f} — today is a net loss despite the cap"
            )
            send_telegram_message(
                cfg,
                f"🏁 Session over — no win today ({risk.stop_reason}).\n"
                f"💰 Net result today: {risk.daily_pnl:+.2f} {cfg.currency}",
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


# ---------------------------------------------------------------------------
# Always-on digit-trigger loop (the default CLI mode)
# ---------------------------------------------------------------------------


def _last_digit(tick: dict) -> str:
    pip_size = tick.get("pip_size", 2)
    quote = float(tick["quote"])
    return f"{quote:.{pip_size}f}"[-1]


def rehydrate_risk(cfg: Config, log_file: Path) -> RiskManager:
    """Rebuilds today's (UTC) risk state from trade_log.csv. Without this, a
    process restart (Render redeploy, crash, platform maintenance) would
    silently reset trades_done/daily_pnl to zero, letting the bot exceed
    MAX_DAILY_LOSS/MAX_ATTEMPTS across a restart. Only counts LIVE rows for
    this exact account/symbol/contract_type/barrier — DRY_RUN quote rows and
    unrelated runs (e.g. --once) don't count."""
    risk = RiskManager(cfg.max_daily_loss, cfg.max_attempts, stop_on_win=False, stop_on_loss=True)
    if not log_file.exists():
        return risk
    today = datetime.now(timezone.utc).date().isoformat()
    with log_file.open("r", newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("timestamp", "").startswith(today):
                continue
            if row.get("mode") != "LIVE":
                continue
            if row.get("account") != cfg.account or row.get("symbol") != cfg.symbol:
                continue
            if row.get("contract_type") != "DIGITDIFF" or row.get("barrier") != cfg.digit_trigger:
                continue
            profit_raw = row.get("profit", "")
            if profit_raw == "":
                continue
            risk.record(float(profit_raw))
    if risk.trades_done:
        print(
            f"Rehydrated today's risk state from {log_file.name}: "
            f"trades_done={risk.trades_done} daily_pnl={risk.daily_pnl:+.2f} won={risk.won} lost={risk.lost}"
        )
    return risk


async def watch_and_trade(cfg: Config, logger: TradeLogger) -> None:
    """Runs forever: stays connected to cfg.symbol's live tick stream and
    fires a DIGITDIFF trade (barrier=cfg.digit_trigger) the instant a tick's
    last digit equals cfg.digit_trigger — through any number of wins, all
    day, stopping for the day the moment one trade loses (RiskManager runs
    with stop_on_win=False, stop_on_loss=True here). MAX_ATTEMPTS and
    MAX_DAILY_LOSS remain as backstops. Once stopped, this keeps watching
    ticks (so it notices the day roll over) but stops placing trades until
    then. Reconnects with exponential backoff on any websocket/API error,
    and tolerates not being logged in yet (missing deriv_tokens.json) by
    waiting and retrying instead of exiting — useful when this runs as a
    server background task that starts before /login has been visited.
    Returns only on cancellation (the caller's job)."""
    mode = "LIVE" if cfg.live_confirm else "DRY_RUN"
    contract_type = "DIGITDIFF"
    print(
        f"=== Deriv digit-trigger bot | {mode} | account={cfg.account} symbol={cfg.symbol} "
        f"trigger_digit={cfg.digit_trigger} stake={cfg.stake} ==="
    )

    current_day = datetime.now(timezone.utc).date()
    risk = rehydrate_risk(cfg, cfg.log_file)
    capped_notified = False
    backoff = 2

    while True:
        client = None
        try:
            tokens = ensure_access_token(load_tokens())
            account_id = pick_account(tokens, cfg.account)
            client = DerivClient(cfg.app_id, tokens["access_token"], account_id)
            await client.connect()
            bal = await client.get_balance()
            print(f"Connected to {bal['loginid']} | balance: {bal['balance']} {bal['currency']}")
            backoff = 2  # reset once a connection actually succeeds

            _, queue = await client.subscribe_ticks(cfg.symbol)
            print(f"Watching {cfg.symbol} ticks for last digit == {cfg.digit_trigger} ...")

            while True:
                today = datetime.now(timezone.utc).date()
                if today != current_day:
                    print(f"New day ({today}) — daily risk counters reset, trading resumes.")
                    current_day = today
                    risk = rehydrate_risk(cfg, cfg.log_file)
                    capped_notified = False

                msg = await queue.get()
                if "error" in msg:
                    raise DerivApiError(msg["error"].get("message", "tick stream error"))

                tick = msg.get("tick")
                if not tick:
                    continue

                if _last_digit(tick) != cfg.digit_trigger:
                    continue

                if not risk.can_trade():
                    if not capped_notified:
                        print(f"Trigger digit seen but not trading — {risk.stop_reason}. Still watching for tomorrow.")
                        capped_notified = True
                    continue

                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                print(f"[{ts}] trigger digit {cfg.digit_trigger} seen on {cfg.symbol} @ {tick['quote']}")

                try:
                    proposal = await client.get_proposal(
                        contract_type, cfg.digit_trigger, cfg.stake, cfg.duration,
                        cfg.duration_unit, cfg.symbol, cfg.currency,
                    )
                    ask_price = float(proposal["ask_price"])
                    payout = float(proposal["payout"])
                except DerivApiError as e:
                    print(f"Proposal error: {e}")
                    continue

                if mode == "DRY_RUN":
                    print(
                        f"[DRY_RUN] {contract_type} barrier={cfg.digit_trigger} stake={ask_price:.2f} "
                        f"payout={payout:.2f} — quote only, no purchase made"
                    )
                    logger.log(
                        timestamp=ts, mode=mode, account=cfg.account, symbol=cfg.symbol,
                        contract_type=contract_type, barrier=cfg.digit_trigger, stake=ask_price, payout=payout,
                    )
                    continue

                print(
                    f"  attempt {risk.trades_done + 1}/{cfg.max_attempts} stake={ask_price:.2f} payout={payout:.2f}"
                )
                try:
                    bought = await client.buy(proposal["id"], ask_price)
                    settled = await client.wait_for_settlement(bought["contract_id"])
                except DerivApiError as e:
                    print(f"Trade error: {e}")
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
                    contract_type=contract_type, barrier=cfg.digit_trigger, stake=ask_price, payout=payout,
                    profit=profit, balance_after=balance_after, contract_id=bought.get("contract_id"),
                )

                result = "WON" if profit > 0 else "LOST"
                print(
                    f"  -> {result} profit={profit:+.2f} | daily_pnl={risk.daily_pnl:+.2f} "
                    f"| attempts={risk.trades_done}/{cfg.max_attempts}"
                )
                trade_desc = describe_contract(contract_type, cfg.digit_trigger)
                headline = (
                    f"✅ Trade won: +{profit:.2f} {cfg.currency}" if profit > 0
                    else f"❌ Trade lost: {profit:.2f} {cfg.currency}"
                )
                send_telegram_message(
                    cfg,
                    f"{headline}\n{cfg.symbol}, {trade_desc}, {cfg.account} account\n"
                    f"📊 Attempt {risk.trades_done} of {cfg.max_attempts} today — running total: "
                    f"{risk.daily_pnl:+.2f} {cfg.currency}",
                )

                if not risk.can_trade() and not capped_notified:
                    capped_notified = True
                    print(
                        f"Trading paused for today ({risk.stop_reason}) | net daily_pnl={risk.daily_pnl:+.2f} "
                        "— still watching, will resume automatically tomorrow."
                    )
                    send_telegram_message(
                        cfg,
                        f"🏁 Trading paused for today — {risk.stop_reason}.\n"
                        f"💰 Net result today: {risk.daily_pnl:+.2f} {cfg.currency}",
                    )

        except SystemExit as e:
            print(f"Not ready to trade yet ({e}) — retrying in {backoff}s. Visit /login if you haven't yet.")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except (DerivApiError, ConnectionClosed, OSError) as e:
            print(f"Connection error: {e} — reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if client:
                await client.close()


async def run_once():
    """Single-shot mode (`--once`): one blind OVER/UNDER session using
    DIRECTION/BARRIER from .env, then exit. This is the old cron-triggered
    behavior, and what server.py's /run endpoint calls."""
    cfg = Config.load()
    mode = "LIVE" if cfg.live_confirm else "DRY_RUN"
    contract_type = "DIGITOVER" if cfg.direction == "OVER" else "DIGITUNDER"
    logger = TradeLogger(cfg.log_file)
    print(f"=== Deriv Over/Under bot (single run) | {mode} | account={cfg.account} stake={cfg.stake} app_id={cfg.app_id} ===")
    try:
        summary = await run_session(cfg, contract_type, mode, logger)
        print(summary)
    finally:
        logger.close()


async def run():
    """CLI entrypoint. Default: runs forever via watch_and_trade (see its
    docstring). Pass --once for a single blind OVER/UNDER session instead."""
    if "--once" in sys.argv:
        await run_once()
        return
    cfg = Config.load()
    logger = TradeLogger(cfg.log_file)
    try:
        await watch_and_trade(cfg, logger)
    finally:
        logger.close()


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        raise SystemExit("Requires Python 3.10+")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
