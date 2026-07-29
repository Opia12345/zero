"""
One-time OAuth2 login for the Deriv API (Authorization Code + PKCE).

Deriv retired the old "paste a static API token" flow for single-connection
websocket trading. Authenticated trading now needs a short-lived OTP-issued
websocket URL, which in turn needs an OAuth2 access token obtained through an
interactive browser login.

Run this script once (and again only if bot_script.py reports the refresh
token no longer works). It opens a browser, you log in and approve, and it
saves an access/refresh token pair plus your account list to
deriv_tokens.json next to this script. bot_script.py refreshes the access
token on its own after that.

Before running, register an app at https://developers.deriv.com/dashboard/apps
with redirect URL exactly:
    http://localhost:8765/callback
and put its App ID in DERIV_APP_ID in .env. The old shared app_id (1089)
won't work here — a redirect_uri has to be registered against a specific app.
"""

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
AUTH_URL = "https://auth.deriv.com/oauth2/auth"
TOKEN_URL = "https://auth.deriv.com/oauth2/token"
ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"

TOKENS_FILE = Path(__file__).with_name("deriv_tokens.json")


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


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def wait_for_callback(expected_state: str) -> dict:
    done = threading.Event()
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parsed.query)
            result["code"] = q.get("code", [None])[0]
            result["state"] = q.get("state", [None])[0]
            result["error"] = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if result["error"]:
                self.wfile.write(f"<h1>Login failed</h1><p>{result['error']}</p>".encode())
            else:
                self.wfile.write(b"<h1>Logged in.</h1><p>Return to the terminal.</p>")
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout=300):
            raise SystemExit("Timed out waiting for the Deriv login callback (5 min).")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    if result.get("error"):
        raise SystemExit(f"Deriv login failed: {result['error']}")
    if not result.get("code") or result.get("state") != expected_state:
        raise SystemExit("Login callback missing an authorization code, or state mismatch — try again.")
    return result


def main():
    env = load_env_file(Path(__file__).with_name(".env"))
    app_id = env.get("DERIV_APP_ID", "").strip()
    if not app_id:
        raise SystemExit("Set DERIV_APP_ID in .env first (your own registered app, not the shared 1089).")

    code_verifier = b64url(secrets.token_bytes(32))
    code_challenge = b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = secrets.token_urlsafe(24)

    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": REDIRECT_URI,
        "scope": "trade",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    print(f"Opening browser for Deriv login:\n{auth_url}\n")
    webbrowser.open(auth_url)

    result = wait_for_callback(state)

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": app_id,
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        timeout=15,
    )
    if not resp.ok:
        raise SystemExit(f"Token exchange failed ({resp.status_code}): {resp.text}")
    tok = resp.json()
    if "access_token" not in tok:
        raise SystemExit(f"Token exchange did not return an access token: {tok}")

    accounts_resp = requests.get(
        ACCOUNTS_URL,
        headers={"Deriv-App-ID": app_id, "Authorization": f"Bearer {tok['access_token']}"},
        timeout=15,
    )
    if not accounts_resp.ok:
        raise SystemExit(f"Fetching accounts failed ({accounts_resp.status_code}): {accounts_resp.text}")
    accounts = accounts_resp.json().get("data", [])
    if not accounts:
        raise SystemExit("No Options trading accounts found on this Deriv account.")

    data = {
        "app_id": app_id,
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + float(tok.get("expires_in", 600)) - 30,
        "accounts": accounts,
    }
    TOKENS_FILE.write_text(json.dumps(data, indent=2))
    TOKENS_FILE.chmod(0o600)

    print(f"Saved tokens to {TOKENS_FILE}\n")
    print("Accounts found:")
    for acc in accounts:
        print(f"  {acc['account_type']:5s} {acc['account_id']}  {acc['balance']} {acc['currency']}  ({acc['status']})")
    print("\nbot_script.py will pick the 'demo' account for DRY_RUN and 'real' for LIVE_CONFIRM=yes.")


if __name__ == "__main__":
    main()
