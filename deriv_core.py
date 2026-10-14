"""Shared Deriv plumbing for the forex/gold bot: .env loading, OAuth2 token
refresh, the websocket client (OTP-issued URL), daily risk caps and Telegram.

Credentials live in a local .env (or real environment variables on Render),
never in code. Deriv's websocket auth is OAuth2 plus a per-connection OTP URL,
so run server.py's /login route once to create the tokens file.
"""

import asyncio
import itertools
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import WebSocketClientProtocol, connect

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


def env_get(key: str, default: str = "") -> str:
    """Real process env vars win over the local .env file."""
    return os.environ.get(key) or load_env_file(Path(__file__).with_name(".env")).get(key, default)


# TOKENS_FILE lets a host with a persistent disk (e.g. Render at /var/data) keep logins across deploys.
TOKENS_FILE = Path(env_get("TOKENS_FILE") or Path(__file__).with_name("deriv_tokens.json"))
OAUTH_AUTH_URL = "https://auth.deriv.com/oauth2/auth"
OAUTH_TOKEN_URL = "https://auth.deriv.com/oauth2/token"
ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"
OTP_URL_TEMPLATE = "https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp"


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
    """Bounds trading for one UTC day: trading stops once cumulative losses
    reach max_daily_loss or the trade count reaches max_attempts. These cap
    the damage; they do not change the odds. Wins do not stop trading unless
    stop_on_win is set."""

    max_daily_loss: float
    max_attempts: int
    stop_on_win: bool = True
    stop_on_loss: bool = False
    profit_target: float = 0.0  # 0 = off; stop for the day once daily_pnl reaches it
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
        elif self.profit_target > 0 and self.daily_pnl >= self.profit_target - 1e-9:
            self.stop_reason = f"hit daily profit target ({self.profit_target})"
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


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------


def send_telegram_message(cfg, text: str) -> None:
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


async def notify(cfg, text: str) -> None:
    """Sends a Telegram message without blocking the event loop (a slow
    Telegram request must not stall tick handling or trade settlement)."""
    await asyncio.to_thread(send_telegram_message, cfg, text)


# ---------------------------------------------------------------------------
# Trade logging
