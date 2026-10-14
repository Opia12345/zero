"""FastAPI wrapper that keeps the forex/gold bot (fx_bot.py) running on Render.

On boot it starts fx_bot.run_forever() as a background task. Routes:
    GET /health      liveness + whether the trading loop is running
    GET /login       one-time Deriv OAuth2 login (see below)
    GET /dashboard   recent trades from the fx trade log

Start command on Render:
    uvicorn server:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
(--proxy-headers makes the OAuth redirect_uri use https behind Render's proxy.)

One-time login (Deriv retired static API tokens for websocket trading):
1. Register an app at https://developers.deriv.com/dashboard/apps with the
   redirect URL exactly: https://<your-render-domain>/oauth/callback
   (for local use: http://localhost:8000/oauth/callback)
2. Visit https://<your-render-domain>/login?app_id=<APP_ID>&api_key=<API_SECRET>
   and approve. It saves the tokens file (TOKENS_FILE); fx_bot refreshes it.
   Without a persistent disk the file is wiped on every deploy/restart, and
   you have to log in again. The bot starts, logs the missing login, and
   retries every few minutes, so logging in after a deploy is enough.

Set API_SECRET so /login and /dashboard are protected. Render's free tier
sleeps after ~15 minutes without HTTP traffic; ping /health every ~10 minutes
(UptimeRobot / cron-job.org) or use a paid instance.
"""

import asyncio
import base64
import csv
import hashlib
import html
import json
import secrets
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import deriv_core as bot
import fx_bot

_trade_task: Optional[asyncio.Task] = None
_cfg: Optional[fx_bot.FxConfig] = None


async def _supervise(cfg: fx_bot.FxConfig):
    """Keeps the trading loop alive. A missing login (SystemExit from the
    token helpers) is retried every 2 minutes so logging in after a deploy
    is enough, with no redeploy."""
    while True:
        try:
            await fx_bot.run_forever(cfg)
        except SystemExit as e:
            print(f"Trading loop waiting: {e}")
        except Exception as e:  # keep the service up; the next pass retries
            print(f"Trading loop crashed: {e!r}")
        await asyncio.sleep(120)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _trade_task, _cfg
    try:
        _cfg = fx_bot.FxConfig.load()
        _trade_task = asyncio.create_task(_supervise(_cfg))
        print(f"Trading loop started ({'LIVE' if _cfg.live_confirm else 'DRY_RUN'}, {_cfg.account}).")
    except SystemExit as e:
        print(f"Trading loop NOT started, fix the environment and redeploy: {e}")
    yield
    if _trade_task:
        _trade_task.cancel()
        try:
            await _trade_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Deriv forex/gold bot", lifespan=lifespan)

# state -> {app_id, code_verifier, redirect_uri}, consumed by /oauth/callback.
_pending_logins: dict[str, dict] = {}


def _configured_secret() -> Optional[str]:
    return bot.env_get("API_SECRET") or None


def _check_api_key(x_api_key: Optional[str], api_key: Optional[str]) -> None:
    secret = _configured_secret()
    if not secret:
        return
    if secret not in (x_api_key, api_key):
        raise HTTPException(status_code=401, detail="missing or invalid API key")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@app.get("/health")
async def health():
    """Ping this from an uptime monitor. `trading` is "running", or "not
    started" (bad environment config, see the logs)."""
    if _trade_task is None:
        trading = "not started"
    elif _trade_task.done():
        trading = f"stopped: {_trade_task.exception()!r}" if not _trade_task.cancelled() else "stopped: cancelled"
    else:
        trading = "running"
    return {
        "status": "ok",
        "trading": trading,
        "mode": None if _cfg is None else ("LIVE" if _cfg.live_confirm else "DRY_RUN"),
        "logged_in": bot.TOKENS_FILE.exists(),
    }


@app.get("/login")
async def login(
    request: Request,
    app_id: str = Query(...),
    api_key: Optional[str] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _check_api_key(x_api_key, api_key)

    code_verifier = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    redirect_uri = str(request.url_for("oauth_callback"))

    _pending_logins[state] = {
        "app_id": app_id,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }

    auth_url = bot.OAUTH_AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": "trade",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return RedirectResponse(auth_url)


@app.get("/oauth/callback", name="oauth_callback")
async def oauth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
):
    if error:
        return HTMLResponse(f"<h1>Login failed</h1><p>{error}: {error_description or ''}</p>", status_code=400)

    pending = _pending_logins.pop(state, None) if state else None
    if not code or not pending:
        return HTMLResponse(
            "<h1>Login failed</h1><p>Missing or expired state — start over at /login.</p>", status_code=400
        )

    resp = requests.post(
        bot.OAUTH_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": pending["app_id"],
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["code_verifier"],
        },
        timeout=15,
    )
    if not resp.ok:
        return HTMLResponse(f"<h1>Token exchange failed</h1><pre>{resp.status_code}: {resp.text}</pre>", status_code=502)
    tok = resp.json()
    if "access_token" not in tok:
        return HTMLResponse(f"<h1>Token exchange failed</h1><pre>{tok}</pre>", status_code=502)

    accounts_resp = requests.get(
        bot.ACCOUNTS_URL,
        headers={"Deriv-App-ID": pending["app_id"], "Authorization": f"Bearer {tok['access_token']}"},
        timeout=15,
    )
    if not accounts_resp.ok:
        return HTMLResponse(
            f"<h1>Fetching accounts failed</h1><pre>{accounts_resp.status_code}: {accounts_resp.text}</pre>",
            status_code=502,
        )
    accounts = accounts_resp.json().get("data", [])
    if not accounts:
        return HTMLResponse("<h1>No Options trading accounts found on this Deriv account.</h1>", status_code=400)

    bot.save_tokens({
        "app_id": pending["app_id"],
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + float(tok.get("expires_in", 600)) - 30,
        "accounts": accounts,
    })

    rows = "".join(
        f"<li>{a['account_type']} {a['account_id']} — {a['balance']} {a['currency']} ({a['status']})</li>"
        for a in accounts
    )
    return HTMLResponse(f"<h1>Logged in.</h1><p>Saved deriv_tokens.json.</p><ul>{rows}</ul>")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    api_key: Optional[str] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _check_api_key(x_api_key, api_key)
    rows: list[dict] = []
    if _cfg is not None and _cfg.log_file.exists():
        with _cfg.log_file.open(newline="") as f:
            rows = list(csv.DictReader(f))
    live = [r for r in rows if r["mode"] == "LIVE" and r["profit"] != ""]
    total = sum(float(r["profit"]) for r in live)
    wins = sum(1 for r in live if float(r["profit"]) > 0)
    head = "".join(f"<th>{h}</th>" for h in fx_bot.LOG_FIELDS)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(r.get(h, ''))}</td>" for h in fx_bot.LOG_FIELDS) + "</tr>"
        for r in reversed(rows[-100:])
    )
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>FX bot</title><style>body{font:14px system-ui;margin:16px}table{border-collapse:collapse}"
        "td,th{border:1px solid #8884;padding:3px 8px;white-space:nowrap}div{overflow-x:auto}</style>"
        f"<h1>FX bot</h1><p>{len(live)} settled trades, {wins} wins, P&amp;L {total:+.2f}</p>"
        f"<div><table><tr>{head}</tr>{body}</table></div>"
    )
