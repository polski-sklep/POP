"""Project-wide test guards. Phase 4.

The build rule is absolute: **no live Spotify or Telegram call, ever, from any
test.** Before this file existed that rule was enforced by a single autouse
fixture inside `tests/test_spotify.py`, which is the one module that could not
have made a live call anyway — it injects `httpx.MockTransport` everywhere.
The four modules that drive `bot.py`, `poll.py` and `triage.py` with fakes had
no guard at all: a fake that quietly fell back to a real client, or a handler
that constructed one, would have gone to the network and the suite would have
gone green (or hung, which is the failure mode the build prompt warned about).

So the guard lives here instead, autouse for every test in the repository:

* name resolution, socket connects and both httpx transports raise instead of
  reaching the network — loud, and in microseconds;
* reading the real `.env` raises, so no test can pick up live credentials by
  accident. Tests build `Settings` with `config.test_settings()`.

`socket.socket` itself is deliberately *not* blocked: asyncio builds a
self-pipe with `socket.socketpair()` on every event loop, and `triage.send()`
legitimately drives a coroutine-returning bot double through `asyncio.run()`.
Blocking the constructor would fail 52 tests that never go near a network.
"""
from __future__ import annotations

import pathlib
import socket
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config  # noqa: E402

REAL_ENV_PATH = config.ENV_PATH


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch):
    """Turn any real network attempt, or any read of the real .env, into a
    failure at the point of the attempt."""

    def blocked_network(*args, **kwargs):
        raise AssertionError(
            "A test attempted a live network call. Tests inject FakeSpotify, "
            "httpx.MockTransport, or a bot double — never a real client."
        )

    monkeypatch.setattr(socket, "getaddrinfo", blocked_network)
    monkeypatch.setattr(socket, "create_connection", blocked_network)
    monkeypatch.setattr(socket.socket, "connect", blocked_network, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_network, raising=False)

    import httpx

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_network)

    real_read_text = pathlib.Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        try:
            is_real_env = self.resolve() == REAL_ENV_PATH.resolve()
        except OSError:  # pragma: no cover — unresolvable path is not .env
            is_real_env = False
        if is_real_env:
            raise AssertionError(
                f"A test read the real {REAL_ENV_PATH}. Use config.test_settings()."
            )
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", guarded_read_text)
    yield
