#!/usr/bin/env python3
"""One-time Spotify consent. Run once on a machine with a browser.

Authorisation Code flow. Spins a loopback callback server, opens the consent
page, exchanges the code, writes SPOTIFY_REFRESH_TOKEN into .env.

Stdlib only, deliberately: Phase 0 must work before any venv exists, and this
file must not depend on modules the later build phases own.

    python3 auth_spotify.py
"""
from __future__ import annotations

import base64
import json
import pathlib
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

SCOPE = "user-read-playback-position"
ENV_PATH = pathlib.Path(__file__).with_name(".env")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


# --- .env -------------------------------------------------------------------

def read_env(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"FAIL  {path} not found. Phase 0 has not been run.")
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env_value(path: pathlib.Path, key: str, value: str) -> None:
    """Replace the key's line if it exists, otherwise append it."""
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


# --- callback server --------------------------------------------------------

class _Callback(BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        _Callback.result = params
        ok = "code" in params
        body = (
            "<h2>POP: consent granted.</h2><p>Close this tab and return to the terminal.</p>"
            if ok else
            f"<h2>POP: consent failed.</h2><pre>{params.get('error', 'unknown error')}</pre>"
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args):
        pass  # keep the terminal clean


def wait_for_code(host: str, port: int, timeout: int = 300) -> dict[str, str]:
    server = HTTPServer((host, port), _Callback)
    server.timeout = 1
    done = threading.Event()

    def serve():
        while not done.is_set():
            server.handle_request()
            if _Callback.result:
                done.set()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    if not done.wait(timeout):
        server.server_close()
        sys.exit(f"FAIL  no callback received within {timeout}s.")
    server.server_close()
    return _Callback.result


# --- main -------------------------------------------------------------------

def main() -> int:
    env = read_env(ENV_PATH)
    client_id = env.get("SPOTIFY_CLIENT_ID", "")
    client_secret = env.get("SPOTIFY_CLIENT_SECRET", "")
    redirect_uri = env.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    if not client_id or not client_secret:
        sys.exit("FAIL  SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET missing from .env")

    if env.get("SPOTIFY_REFRESH_TOKEN"):
        print("NOTE  SPOTIFY_REFRESH_TOKEN already set; re-consenting will overwrite it.")

    parts = urllib.parse.urlparse(redirect_uri)
    host, port = parts.hostname or "127.0.0.1", parts.port or 8888

    state = secrets.token_urlsafe(24)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "show_dialog": "true",
    })

    print(f"\nListening on {host}:{port} for the callback.")
    print("Opening your browser. If nothing opens, paste this URL yourself:\n")
    print(url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    params = wait_for_code(host, port)

    if "error" in params:
        sys.exit(f"FAIL  Spotify returned error={params['error']}")
    if params.get("state") != state:
        sys.exit("FAIL  state mismatch — discarding response. Re-run and do not reuse an old tab.")
    code = params.get("code")
    if not code:
        sys.exit("FAIL  no authorisation code in callback.")

    print("Code received. Exchanging for tokens…")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.exit(f"FAIL  token exchange HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"FAIL  token exchange could not reach Spotify: {e.reason}")

    refresh = payload.get("refresh_token")
    if not refresh:
        sys.exit(f"FAIL  no refresh_token in response. Keys returned: {sorted(payload)}")

    granted = payload.get("scope", "")
    if SCOPE not in granted.split():
        sys.exit(
            f"FAIL  required scope '{SCOPE}' was not granted (got: {granted!r}).\n"
            "      Without it every resume_point reads null and measurement is dead.\n"
            "      Re-run and accept the full consent screen."
        )

    write_env_value(ENV_PATH, "SPOTIFY_REFRESH_TOKEN", refresh)
    print(f"\nOK    refresh token stored in .env ({len(refresh)} chars)")
    print(f"OK    scope granted: {granted}")
    print("\nNext: python3 check_env.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
