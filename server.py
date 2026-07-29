"""
HTTP wrapper around bot_script.run_session(), meant to be triggered by an
external cron scheduler (e.g. a Render Cron Job, or any scheduler that can
make an HTTP call) hitting this service's deployed URL.

There is no scheduling inside this app — Render web services just keep
running and answering requests, they don't run things "at 1am" by
themselves. Something outside the app has to call POST /run at the time you
want a session to fire.

Deploy on Render as a Web Service with:
    Start command: uvicorn server:app --host 0.0.0.0 --port $PORT

Trigger a session:
    POST /run?app_id=<id>&stake=<amount>&live_confirm=yes|no
    (all three are optional — omitted ones fall back to .env)

Protect it: set API_SECRET in the environment. Requests must then send it as
either header `X-API-Key: <secret>` or query param `?api_key=<secret>`. If
API_SECRET isn't set, the endpoint is unauthenticated — fine for local
testing, not for a deployed app that can place real trades.

Known limitation: deriv_tokens.json (written by deriv_login.py) must exist on
the server's filesystem. Render web services have ephemeral disks by default
— use a persistent disk, or Render's "Secret Files" feature to seed the file
on deploy, or re-run deriv_login.py against the deployed instance if you add
a login route later.
"""

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

import bot_script as bot

app = FastAPI(title="Deriv Over/Under bot")


def _configured_secret() -> Optional[str]:
    file_env = bot.load_env_file(Path(__file__).with_name(".env"))
    return os.environ.get("API_SECRET") or file_env.get("API_SECRET") or None


def _check_api_key(x_api_key: Optional[str], api_key: Optional[str]) -> None:
    secret = _configured_secret()
    if not secret:
        return
    if secret not in (x_api_key, api_key):
        raise HTTPException(status_code=401, detail="missing or invalid API key")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.api_route("/run", methods=["GET", "POST"])
async def run(
    app_id: Optional[str] = Query(default=None),
    stake: Optional[float] = Query(default=None),
    live_confirm: Optional[str] = Query(default=None),
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
