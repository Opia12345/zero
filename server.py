"""
HTTP wrapper around bot_script.run_session(), meant to be triggered by an
external cron scheduler (e.g. a Render Cron Job, or any scheduler that can
make an HTTP call) hitting this service's deployed URL.

There is no scheduling inside this app — Render web services just keep
running and answering requests, they don't run things "at 1am" by
themselves. Something outside the app has to call POST /run at the time you
want a session to fire.

Deploy on Render as a Web Service with:
    Start command: uvicorn server:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
(--proxy-headers is required so the OAuth login flow below sees "https",
not "http", behind Render's proxy — otherwise the redirect_uri it builds
won't match what's registered with Deriv.)

One-time login (Deriv retired static API tokens for websocket trading; this
now needs an interactive OAuth2 + PKCE login):
1. Register an app at https://developers.deriv.com/dashboard/apps with
   redirect URL exactly: https://<your-render-domain>/oauth/callback
2. Visit https://<your-render-domain>/login?app_id=<that app's ID>&api_key=<API_SECRET>
   in a browser, log in, approve. It saves deriv_tokens.json on the server;
   bot_script.py refreshes it automatically after that.
Known limitation: Render web services have ephemeral disks by default, so a
redeploy wipes deriv_tokens.json — you'd need to hit /login again after one.

Trigger a session:
    POST /run?app_id=<id>&stake=<amount>&live_confirm=yes|no
    (all three are optional — omitted ones fall back to .env)

Protect it: set API_SECRET in the environment. Requests must then send it as
either header `X-API-Key: <secret>` or query param `?api_key=<secret>`. If
API_SECRET isn't set, both /run and /login are unauthenticated — fine for
local testing, not for a deployed app that can place real trades.
"""

import base64
import csv
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import bot_script as bot
from dashboard import render_dashboard

app = FastAPI(title="Deriv Over/Under bot")

# state -> {app_id, code_verifier, redirect_uri}, cleared once consumed by
# /oauth/callback. Fine as an in-memory dict: this app runs as a single
# worker process, and only one login happens at a time.
_pending_logins: dict[str, dict] = {}


def _configured_secret() -> Optional[str]:
    file_env = bot.load_env_file(Path(__file__).with_name(".env"))
    return os.environ.get("API_SECRET") or file_env.get("API_SECRET") or None


def _check_api_key(x_api_key: Optional[str], api_key: Optional[str]) -> None:
    secret = _configured_secret()
    if not secret:
        return
    if secret not in (x_api_key, api_key):
        raise HTTPException(status_code=401, detail="missing or invalid API key")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@app.get("/health")
async def health():
    return {"status": "ok"}


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


@app.api_route("/run", methods=["GET", "POST"])
async def run(
    app_id: Optional[str] = Query(default=None),
    stake: Optional[float] = Query(default=None),
    live_confirm: Optional[str] = Query(default=None),
    account: Optional[str] = Query(default=None, description="'demo' or 'real' — which Deriv account to trade against"),
    api_key: Optional[str] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _check_api_key(x_api_key, api_key)

    overrides = {}
    if app_id is not None:
        overrides["DERIV_APP_ID"] = app_id
    if stake is not None:
        overrides["STAKE"] = str(stake)
    if live_confirm is not None:
        overrides["LIVE_CONFIRM"] = live_confirm
    if account is not None:
        overrides["ACCOUNT"] = account

    try:
        cfg = bot.Config.load(overrides)
    except SystemExit as e:
        raise HTTPException(status_code=400, detail=str(e))

    mode = "LIVE" if cfg.live_confirm else "DRY_RUN"
    contract_type = "DIGITOVER" if cfg.direction == "OVER" else "DIGITUNDER"
    logger = bot.TradeLogger(cfg.log_file)
    try:
        summary = await bot.run_session(cfg, contract_type, mode, logger)
    except SystemExit as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        logger.close()

    return JSONResponse(summary)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    api_key: Optional[str] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _check_api_key(x_api_key, api_key)

    file_env = bot.load_env_file(Path(__file__).with_name(".env"))
    log_name = os.environ.get("LOG_FILE") or file_env.get("LOG_FILE") or "trade_log.csv"
    log_path = Path(__file__).with_name(log_name)

    rows: list = []
    if log_path.exists():
        with log_path.open("r", newline="") as f:
            rows = list(csv.DictReader(f))

    accounts: list = []
    if bot.TOKENS_FILE.exists():
        try:
            accounts = json.loads(bot.TOKENS_FILE.read_text()).get("accounts", [])
        except json.JSONDecodeError:
            accounts = []

    return HTMLResponse(render_dashboard(rows, accounts))
