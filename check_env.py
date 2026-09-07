#!/usr/bin/env python3
"""Phase 0 gate. Validates .env is complete and that both credentials authenticate.

    python3 check_env.py           # validate + live auth checks
    python3 check_env.py --ping    # also send a test message to TELEGRAM_USER_ID

Exit 0 = every check passed. Anything else = do not proceed with the build.
Secrets are never printed; only lengths and prefixes.
"""
from __future__ import annotations

import base64
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = pathlib.Path(__file__).with_name(".env")
REQUIRED_SCOPE = "user-read-playback-position"

REQUIRED = {
    "SPOTIFY_CLIENT_ID":      r"[0-9a-f]{32}",
    "SPOTIFY_CLIENT_SECRET":  r"[0-9a-f]{32}",
    "SPOTIFY_REFRESH_TOKEN":  r"\S{20,}",
    "SPOTIFY_REDIRECT_URI":   r"https?://\S+",
    "TELEGRAM_BOT_TOKEN":     r"\d{6,12}:[A-Za-z0-9_-]{30,}",
    "TELEGRAM_USER_ID":       r"\d{5,15}",
    "WEEKLY_ALLOWANCE_MIN":   r"\d+",
    "POP_TZ":                 r"\S+/\S+",
    "LOCK_HOURS":             r"\d+",
    "LIFESPAN_CYCLES":        r"\d+",
    "POP_DB_PATH":            r"\S+",
}
SECRET_KEYS = {
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
    "SPOTIFY_REFRESH_TOKEN", "TELEGRAM_BOT_TOKEN",
}

failures: list[str] = []


def mask(key: str, val: str) -> str:
    return f"{val[:4]}…[{len(val)}]" if key in SECRET_KEYS else val


def ok(msg: str) -> None:
    print(f"  OK    {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        print(f"FAIL  {ENV_PATH} does not exist.")
        raise SystemExit(2)
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def check_presence(env: dict[str, str]) -> None:
    print("\n[1/4] .env completeness and shape")
    for key, pattern in REQUIRED.items():
        val = env.get(key, "")
        if not val:
            fail(f"{key} is missing or empty")
        elif not re.fullmatch(pattern, val):
            fail(f"{key} does not match expected shape /{pattern}/ (got {mask(key, val)})")
        else:
            ok(f"{key} = {mask(key, val)}")


def check_permissions() -> None:
    print("\n[2/4] file hygiene")
    mode = ENV_PATH.stat().st_mode & 0o777
    (ok if mode == 0o600 else fail)(f".env mode is {oct(mode)}" + ("" if mode == 0o600 else " (want 0o600)"))
    gi = ENV_PATH.with_name(".gitignore")
    if not gi.exists():
        fail(".gitignore missing")
        return
    body = gi.read_text()
    for pat in (".env", "data/", "__pycache__/", "*.session"):
        (ok if pat in body else fail)(f".gitignore covers {pat}")


def check_telegram(env: dict[str, str], ping: bool) -> None:
    print("\n[3/4] Telegram — live getMe")
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        fail("no token to test")
        return
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=20
        ) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        fail(f"getMe HTTP {e.code}: {body[:200]}")
        return
    except urllib.error.URLError as e:
        fail(f"getMe unreachable: {e.reason}")
        return
    if not data.get("ok"):
        fail(f"getMe returned ok=false: {data}")
        return
    me = data["result"]
    ok(f"authenticated as @{me.get('username')} (id {me.get('id')})")
    declared = env.get("TELEGRAM_BOT_USERNAME")
    if declared and declared != me.get("username"):
        fail(f"TELEGRAM_BOT_USERNAME={declared} but token belongs to @{me.get('username')}")

    if ping:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=urllib.parse.urlencode({
                    "chat_id": env["TELEGRAM_USER_ID"],
                    "text": "POP: check_env.py ping. Credentials work. Nothing else is running yet.",
                }).encode(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                pong = json.load(r)
            if pong.get("ok"):
                ok(f"test message delivered to {env['TELEGRAM_USER_ID']}")
            else:
                fail(f"sendMessage returned: {pong}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            fail(f"sendMessage HTTP {e.code}: {body[:200]} "
                 "(if 403, press Start in the bot chat first)")


def check_spotify(env: dict[str, str]) -> None:
    print("\n[4/4] Spotify — live refresh-token exchange")
    cid, csec = env.get("SPOTIFY_CLIENT_ID", ""), env.get("SPOTIFY_CLIENT_SECRET", "")
    refresh = env.get("SPOTIFY_REFRESH_TOKEN", "")
    if not (cid and csec and refresh):
        fail("client id/secret/refresh token incomplete — run auth_spotify.py")
        return
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh,
        }).encode(),
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        fail(f"refresh HTTP {e.code}: {body[:300]}")
        return
    except urllib.error.URLError as e:
        fail(f"Spotify unreachable: {e.reason}")
        return

    tok = payload.get("access_token")
    if not tok:
        fail(f"no access_token returned; keys={sorted(payload)}")
        return
    ok(f"access token minted ({len(tok)} chars, expires_in={payload.get('expires_in')}s)")

    granted = payload.get("scope", "").split()
    if REQUIRED_SCOPE in granted:
        ok(f"scope '{REQUIRED_SCOPE}' present — resume_point is readable")
    else:
        fail(f"scope '{REQUIRED_SCOPE}' NOT granted (got {granted}); "
             "measurement would silently read null. Re-run auth_spotify.py")


def main() -> int:
    ping = "--ping" in sys.argv
    print("POP — Phase 0 credential check")
    env = load_env()
    check_presence(env)
    check_permissions()
    check_telegram(env, ping)
    check_spotify(env)

    print("\n" + "=" * 60)
    if failures:
        print(f"RESULT: FAILED — {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        print("Do not start the build until these are resolved.")
        return 1
    print("RESULT: PASSED — .env is complete and both credentials authenticate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
