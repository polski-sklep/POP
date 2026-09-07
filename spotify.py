"""Spotify: reference parsing, refresh-token auth, episode fetch, the clamp.

Phase 2, Agent B. Implements CONTRACTS.md §3 exactly.

This is the only module in the codebase that performs HTTP. Everything else
takes a `SpotifyClient`-shaped object so tests can inject `FakeSpotify`.

Two things in here are load-bearing and easy to get silently wrong:

1. `clamped_delta`. `resume_point` is a POSITION, not a total. Scrubbing back
   or relistening drops it, and an unclamped delta goes negative, crediting
   time that was never earned back.
2. A missing `resume_point`. That means the token lost the
   `user-read-playback-position` scope. Read as zero it is indistinguishable
   from "listened to nothing", which is the one failure this system cannot
   afford to swallow — so it raises.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, replace
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

import httpx

from config import Settings

__all__ = [
    "SpotifyError",
    "InvalidEpisodeRef",
    "Episode",
    "EPISODE_ID_RE",
    "parse_episode_ref",
    "SpotifyClient",
    "clamped_delta",
    "FakeSpotify",
    "strip_html",
]


class SpotifyError(Exception):
    """Anything wrong with the API, the token, or the shape of a response."""


class SpotifyForbidden(SpotifyError):
    """HTTP 403. Subclass so existing `except SpotifyError` handlers still catch it."""


class SpotifyScopeError(SpotifyError):
    """The token lost `user-read-playback-position`. Never swallowed anywhere."""


log = logging.getLogger("pop.spotify")


class InvalidEpisodeRef(ValueError):
    """Message is user-facing: the bot shows `str(exc)` verbatim."""


@dataclass(frozen=True)
class Episode:
    spotify_id: str
    title: str
    show: str
    description: str | None
    release_date: str | None
    duration_ms: int
    resume_position_ms: int
    fully_played: bool


# --- parsing ----------------------------------------------------------------

EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")

_SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com"}
# open.spotify.com/intl-pt/episode/<id>, and the odd intl-pt-br form.
_LOCALE_RE = re.compile(r"^intl-[a-z]{2}(-[a-z]{2})?$", re.IGNORECASE)

_HOW = (
    "Send a Spotify episode link — either "
    "https://open.spotify.com/episode/<id> or spotify:episode:<id>."
)

# Named so the rejection can say what the link actually is. Silent rejection
# is forbidden by the brief.
_TYPE_NAMES = {
    "track": "a track",
    "album": "an album",
    "playlist": "a playlist",
    "artist": "an artist",
    "show": "a show",
    "user": "a user profile",
    "audiobook": "an audiobook",
    "chapter": "an audiobook chapter",
    "collection": "a library collection",
    "local": "a local file",
    "search": "a search page",
}


def _name_for(kind: str) -> str:
    return _TYPE_NAMES.get(kind.lower(), f"a Spotify {kind}")


def _wrong_type(kind: str) -> InvalidEpisodeRef:
    extra = ""
    if kind.lower() == "show":
        extra = (" That's the whole podcast — open the episode you want "
                 "and share that link instead.")
    return InvalidEpisodeRef(
        f"That's {_name_for(kind)}, not a podcast episode.{extra} {_HOW}"
    )


def _bad_id(ident: str) -> InvalidEpisodeRef:
    return InvalidEpisodeRef(
        f"That looks like an episode link, but {ident!r} is not a valid "
        f"episode id — they are exactly 22 letters and digits "
        f"(that one is {len(ident)})."
    )


class _Candidate:
    """A Spotify-looking token: either a resolved episode id, or the error to
    raise if nothing better turns up later in the message."""

    __slots__ = ("episode_id", "error")

    def __init__(self, episode_id: str | None = None,
                 error: InvalidEpisodeRef | None = None) -> None:
        self.episode_id = episode_id
        self.error = error


# Wrapping punctuation a link picks up in prose: "(https://…)", "…/ID>.", etc.
# ':' is deliberately absent so a malformed "spotify::" is still recognised.
_LEAD = "([{<\"'\u00ab\u201c\u2018"
_TRAIL = ".,;!?)]}>\"'\u00bb\u201d\u2019"


def _tokens(text: str) -> list[str]:
    out = []
    for raw in text.split():
        tok = raw.strip(_LEAD + _TRAIL) if len(raw) > 1 else raw
        if tok:
            out.append(tok)
    return out


def parse_episode_ref(text: str) -> str:
    """Return the bare 22-character base62 episode id.

    Two accepted forms, per CONTRACTS.md §3:

      * ``https://open.spotify.com/episode/<id>`` — query string and fragment
        stripped, optional locale segment (``/intl-pt/episode/<id>``) and
        optional scheme tolerated
      * ``spotify:episode:<id>``

    Either may be **embedded in surrounding text**. Sharing from Spotify's iOS
    share sheet into Telegram prepends the episode title and show name, and
    bouncing a share that contains a perfectly good link — making the user go
    back and re-copy it — is exactly the admin burden this tool dies from. So
    the first valid episode reference anywhere in the message wins, even if a
    wrong-type Spotify link appears before it.

    That is tolerance of a real share format, not tolerance of malformed
    input. Anything with no valid episode reference in it still raises
    `InvalidEpisodeRef`, with a message naming what was actually found, which
    the bot shows verbatim. A bare 22-character id is still rejected: it is
    not one of the two accepted forms and it matches ordinary words too
    easily.
    """
    if not isinstance(text, str):
        raise InvalidEpisodeRef(f"That isn't a link I can read. {_HOW}")

    ref = text.strip()
    if not ref:
        raise InvalidEpisodeRef(f"That message had no link in it. {_HOW}")

    deferred: list[InvalidEpisodeRef] = []
    for token in _tokens(ref):
        cand = _classify(token)
        if cand is None:
            continue
        if cand.episode_id is not None:
            return cand.episode_id          # first valid episode ref wins
        if cand.error is not None:
            deferred.append(cand.error)

    # No episode reference anywhere. Report what was actually found.
    if deferred:
        raise deferred[0]
    if EPISODE_ID_RE.match(ref):
        # Unambiguous to us, but not one of the two accepted forms, and a bare
        # scan for these would match ordinary 22-character words.
        raise InvalidEpisodeRef(
            f"That looks like a bare episode id. I need the whole link. {_HOW}"
        )
    raise InvalidEpisodeRef(f"That isn't a Spotify link. {_HOW}")


def _classify(token: str) -> _Candidate | None:
    low = token.lower()
    if low.startswith("spotify:"):
        return _classify_uri(token)
    if low.startswith(("http://", "https://")) or "spotify.com" in low:
        return _classify_url(token)
    return None


def _classify_uri(token: str) -> _Candidate:
    malformed = InvalidEpisodeRef(f"That Spotify URI is malformed. {_HOW}")
    parts = token.split(":")
    if len(parts) < 3 or not parts[1]:
        return _Candidate(error=malformed)
    kind = parts[1].lower()
    if kind != "episode":
        return _Candidate(error=_wrong_type(kind))
    if len(parts) != 3:
        return _Candidate(error=malformed)
    ident = parts[2]
    if not EPISODE_ID_RE.match(ident):
        return _Candidate(error=_bad_id(ident))
    return _Candidate(episode_id=ident)


def _classify_url(token: str) -> _Candidate:
    raw = token if "://" in token else "https://" + token
    try:
        parts = urlsplit(raw)
    except ValueError:  # pragma: no cover - urlsplit rarely raises
        return _Candidate(
            error=InvalidEpisodeRef(f"I couldn't read that link. {_HOW}"))

    host = parts.netloc.lower().rsplit("@", 1)[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host not in _SPOTIFY_HOSTS:
        shown = host or token
        return _Candidate(error=InvalidEpisodeRef(
            f"That's a link to {shown}, not Spotify. {_HOW}"))

    # urlsplit already dropped ?query and #fragment.
    segments = [s for s in parts.path.split("/") if s]
    if segments and _LOCALE_RE.match(segments[0]):
        segments = segments[1:]
    if segments and segments[0].lower() == "embed":
        segments = segments[1:]
    if len(segments) < 2:
        return _Candidate(error=InvalidEpisodeRef(
            f"That Spotify link doesn't point at anything I can queue. {_HOW}"))

    kind, ident = segments[0].lower(), segments[1]
    if kind != "episode":
        return _Candidate(error=_wrong_type(kind))
    if not EPISODE_ID_RE.match(ident):
        return _Candidate(error=_bad_id(ident))
    return _Candidate(episode_id=ident)


# --- the clamp --------------------------------------------------------------

def clamped_delta(previous_position_ms: int, current_position_ms: int) -> int:
    """Listening time between two polls, floored at zero.

    `resume_point` is a position, not a total. Scrubbing backward or starting
    a relisten drops it; an unclamped delta would go negative and silently
    credit time back to the allowance.
    """
    return max(0, current_position_ms - previous_position_ms)


# --- HTML stripping ---------------------------------------------------------

class _Stripper(HTMLParser):
    # Paragraph-level tags open a blank line; line-level tags a single break.
    _PARA = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    _LINE = {"br", "li", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._PARA:
            self.parts.append("\n\n")
        elif tag in self._LINE:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        # <br/> must not count as both an open and a close.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._PARA:
            self.parts.append("\n\n")


def strip_html(text: str | None) -> str | None:
    """Plain text from Spotify's HTML-ish description. None stays None."""
    if text is None:
        return None
    parser = _Stripper()
    parser.feed(text)
    parser.close()
    out = unescape("".join(parser.parts))
    out = re.sub(r"[ \t\r\f\v]+", " ", out)
    out = "\n".join(line.strip() for line in out.split("\n"))
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# --- the client -------------------------------------------------------------

MAX_IDS_PER_REQUEST = 50   # the API maximum for GET /v1/episodes
_TOKEN_LEEWAY_S = 60       # renew a minute early rather than race the expiry


def _chunk(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class SpotifyClient:
    """Refresh-token-only Spotify client.

    The browser consent already happened (`auth_spotify.py`); this class never
    opens one. The access token is cached in memory with its expiry and
    renewed transparently. A 401 triggers exactly one forced-refresh retry.

    `transport` is injected so tests use `httpx.MockTransport`. Nothing in the
    test suite ever reaches the network.
    """

    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API_BASE = "https://api.spotify.com/v1"

    def __init__(self, settings: Settings, *, transport: Any = None) -> None:
        self.settings = settings
        self._access_token: str | None = None
        #: Latched when the batch /episodes endpoint 403s; see get_episodes().
        self._batch_forbidden = False
        self._expires_at: float = 0.0
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=False,
        )

    # -- lifecycle
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- auth
    @staticmethod
    def _monotonic() -> float:
        """Elapsed-time seam. Monotonic, so it is not wall-clock dependent and
        does not need the injected Clock (which is for stored timestamps)."""
        return time.monotonic()

    def access_token(self) -> str:
        """Cached access token, refreshed on first use and on expiry."""
        if self._access_token is not None and self._monotonic() < self._expires_at:
            return self._access_token
        return self._refresh()

    def _invalidate(self) -> None:
        self._access_token = None
        self._expires_at = 0.0

    def _refresh(self) -> str:
        if not self.settings.spotify_refresh_token:
            raise SpotifyError(
                "No Spotify refresh token configured — run auth_spotify.py once."
            )
        basic = base64.b64encode(
            f"{self.settings.spotify_client_id}:"
            f"{self.settings.spotify_client_secret}".encode()
        ).decode()
        try:
            resp = self._client.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.settings.spotify_refresh_token,
                },
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except httpx.HTTPError as exc:
            raise SpotifyError(f"Token refresh failed: {exc}") from exc

        if resp.status_code != 200:
            raise SpotifyError(
                f"Token refresh rejected: HTTP {resp.status_code} "
                f"{_body_snippet(resp)}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SpotifyError("Token refresh returned non-JSON.") from exc

        token = payload.get("access_token")
        if not token:
            raise SpotifyError(
                f"Token refresh returned no access_token: {payload!r}"
            )
        # Spotify occasionally rotates the refresh token. We cannot persist it
        # (.env is written by auth_spotify.py only), so it is used for the
        # lifetime of this process and then discarded.
        expires_in = int(payload.get("expires_in", 3600) or 3600)
        self._access_token = str(token)
        self._expires_at = self._monotonic() + max(0, expires_in - _TOKEN_LEEWAY_S)
        return self._access_token

    # -- requests
    def _authorised_get(self, url: str, params: dict[str, str] | None) -> httpx.Response:
        try:
            return self._client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.access_token()}"},
            )
        except httpx.HTTPError as exc:
            raise SpotifyError(f"Request to {url} failed: {exc}") from exc

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        url = f"{self.API_BASE}{path}"
        resp = self._authorised_get(url, params)
        if resp.status_code == 401:
            # Exactly one forced-refresh retry, then give up.
            self._invalidate()
            resp = self._authorised_get(url, params)
            if resp.status_code == 401:
                raise SpotifyError(
                    "Spotify returned 401 twice, after a forced token refresh. "
                    f"{_body_snippet(resp)}"
                )
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After", "?")
            raise SpotifyError(f"Spotify rate limited the request (429); "
                               f"Retry-After {retry}.")
        if resp.status_code == 403:
            raise SpotifyForbidden(
                f"Spotify returned HTTP 403 for {path} {_body_snippet(resp)}"
            )
        if resp.status_code >= 400:
            raise SpotifyError(
                f"Spotify returned HTTP {resp.status_code} for {path} "
                f"{_body_snippet(resp)}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise SpotifyError(f"Spotify returned non-JSON for {path}.") from exc

    # -- episodes
    def get_episode(self, spotify_id: str) -> Episode:
        return _episode_from_json(self._get(f"/episodes/{spotify_id}"))

    def get_episodes(self, ids: list[str]) -> list[Episode]:
        """Batched at the API maximum of 50 ids per request.

        Nulls in the response array (unavailable or unknown ids) are skipped,
        so the result may be shorter than `ids`.

        Falls back to one request per id if the batch endpoint returns 403 —
        see `_fetch_singly`.
        """
        wanted = list(ids)
        if not wanted:
            return []
        if self._batch_forbidden:
            return self._fetch_singly(wanted)

        out: list[Episode] = []
        for batch in _chunk(wanted, MAX_IDS_PER_REQUEST):
            try:
                payload = self._get("/episodes", {"ids": ",".join(batch)})
            except SpotifyForbidden:
                # Observed against the live API on 2026-08-30: GET /episodes/{id}
                # returns 200 while GET /episodes?ids=... returns 403 for the
                # same token and the same episode, with or without `market`.
                # A Spotify-side restriction on the several-episodes endpoint,
                # not something this client can fix by asking differently.
                #
                # The brief already budgets for this: "Cost is one API call per
                # queued episode." Latch the flag so a queue of 30 episodes
                # costs one wasted request per process, not thirty.
                log.warning(
                    "Spotify refused the batch /episodes endpoint (403); "
                    "falling back to one request per episode for this process."
                )
                self._batch_forbidden = True
                return self._fetch_singly(wanted)

            items = payload.get("episodes")
            if items is None:
                raise SpotifyError(
                    "Spotify response had no 'episodes' array: "
                    f"{sorted(payload)!r}"
                )
            for item in items:
                if item is None:
                    continue
                out.append(_episode_from_json(item))
        return out

    def _fetch_singly(self, ids: list[str]) -> list[Episode]:
        """One request per id, tolerating individual failures.

        Mirrors the batch contract: unknown or unavailable ids are skipped
        rather than raising, so the result may be shorter than `ids` and
        callers must key by `spotify_id` rather than zipping by index.

        A missing `resume_point` still raises — that means the token lost the
        user-read-playback-position scope, and swallowing it would report the
        user as listening to nothing.
        """
        out: list[Episode] = []
        for spotify_id in ids:
            try:
                out.append(self.get_episode(spotify_id))
            except SpotifyScopeError:
                raise
            except SpotifyError as exc:
                log.warning("skipping episode %s: %s", spotify_id, exc)
        return out


def _body_snippet(resp: httpx.Response, limit: int = 200) -> str:
    try:
        text = resp.text
    except Exception:  # pragma: no cover
        return ""
    text = " ".join(text.split())
    return text[:limit]


def _episode_from_json(data: dict) -> Episode:
    spotify_id = data.get("id")
    if not spotify_id:
        raise SpotifyError(f"Episode object has no id: {sorted(data)!r}")

    resume = data.get("resume_point")
    if not isinstance(resume, dict):
        raise SpotifyScopeError(
            f"resume_point missing from episode {spotify_id}. The access token "
            "has lost the user-read-playback-position scope — re-run "
            "auth_spotify.py. Refusing to report this as zero listening."
        )
    if "resume_position_ms" not in resume or "fully_played" not in resume:
        raise SpotifyError(
            f"resume_point for episode {spotify_id} is incomplete "
            f"({sorted(resume)!r}). Refusing to guess at a position."
        )

    show = data.get("show") or {}
    return Episode(
        spotify_id=str(spotify_id),
        title=str(data.get("name") or ""),
        show=str(show.get("name") or ""),
        description=strip_html(data.get("description")),
        release_date=data.get("release_date"),
        duration_ms=int(data.get("duration_ms") or 0),
        resume_position_ms=int(resume.get("resume_position_ms") or 0),
        fully_played=bool(resume.get("fully_played")),
    )


# --- test double ------------------------------------------------------------

class FakeSpotify:
    """Same surface as `SpotifyClient`, driven by a dict of Episode fixtures.

    Agents C and the Phase 3/4 work import this, never the real client. It
    performs no I/O of any kind.
    """

    def __init__(self, episodes: dict[str, Episode]):
        self.episodes: dict[str, Episode] = dict(episodes)
        self.calls: list[tuple[str, Any]] = []
        self.token_refreshes: int = 0

    # -- fixture control
    def set_position(self, spotify_id: str, position_ms: int,
                     fully_played: bool = False) -> None:
        """Move an episode's resume_point, as a listen would."""
        ep = self.episodes.get(spotify_id)
        if ep is None:
            raise KeyError(
                f"FakeSpotify has no episode {spotify_id!r}; "
                f"known: {sorted(self.episodes)!r}"
            )
        self.episodes[spotify_id] = replace(
            ep, resume_position_ms=int(position_ms),
            fully_played=bool(fully_played),
        )

    def add_episode(self, episode: Episode) -> None:
        self.episodes[episode.spotify_id] = episode

    # -- SpotifyClient surface
    def access_token(self) -> str:
        self.token_refreshes += 1
        return "fake-access-token"

    def get_episode(self, spotify_id: str) -> Episode:
        self.calls.append(("get_episode", spotify_id))
        ep = self.episodes.get(spotify_id)
        if ep is None:
            raise SpotifyError(f"No such episode: {spotify_id}")
        return ep

    def get_episodes(self, ids: list[str]) -> list[Episode]:
        wanted = list(ids)
        self.calls.append(("get_episodes", wanted))
        # Mirrors the real client: unknown ids come back as nulls and are
        # skipped, so the result may be shorter than the request.
        return [self.episodes[i] for i in wanted if i in self.episodes]

    def close(self) -> None:
        return None
