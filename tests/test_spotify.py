"""Tests for spotify.py — Agent B.

ABSOLUTE RULE: no test in this file may reach the network. Every client is
built with an injected `httpx.MockTransport`, and the `no_live_http` autouse
fixture below turns any real socket attempt into an immediate failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                    # noqa: E402
import spotify                                   # noqa: E402
from spotify import (                            # noqa: E402
    Episode,
    FakeSpotify,
    InvalidEpisodeRef,
    SpotifyClient,
    SpotifyError,
    clamped_delta,
    parse_episode_ref,
)
from tests.fixtures_spotify import (             # noqa: E402
    EPISODES,
    FINISHED_ID,
    LONG_ID,
    MEDIUM_ID,
    PARTIAL_ID,
    SHORT_ID,
    TOKEN_JSON,
    UNKNOWN_ID,
    episode_json,
    fake_spotify,
    make_episode,
    make_ids,
)

MIN = 60_000


# ---------------------------------------------------------------------------
# Safety net: nothing here is allowed to open a real connection.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError(
            "a test tried to make a real HTTP request — inject a transport"
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)


# ---------------------------------------------------------------------------
# THE CLAMP. The part of this system most likely to be silently wrong.
# ---------------------------------------------------------------------------

def test_clamp_forward_listening():
    """Ten minutes of forward progress credits ten minutes."""
    assert clamped_delta(5 * MIN, 15 * MIN) == 10 * MIN


def test_clamp_backward_scrub():
    """Scrubbing back must credit zero, never a negative refund."""
    delta = clamped_delta(30 * MIN, 10 * MIN)
    assert delta == 0
    assert delta >= 0, "a negative delta silently gives the allowance back"


def test_clamp_relisten_from_zero():
    """Restarting an episode drops the position to 0 — still zero credit."""
    assert clamped_delta(45 * MIN, 0) == 0


def test_clamp_no_change():
    """Two polls with no listening in between credit nothing."""
    assert clamped_delta(17 * MIN, 17 * MIN) == 0


def test_clamp_equal_positions():
    """Equality is zero at any magnitude, not just small ones."""
    for pos in (1, 999, 60_000, 9_999_999):
        assert clamped_delta(pos, pos) == 0


def test_clamp_zero_to_zero():
    """Never started, still not started."""
    assert clamped_delta(0, 0) == 0


def test_clamp_first_poll_from_zero():
    """The very first poll on a fresh episode credits everything heard."""
    assert clamped_delta(0, 3 * MIN) == 3 * MIN


def test_clamp_is_never_negative_over_a_grid():
    for prev in range(0, 400_000, 37_000):
        for cur in range(0, 400_000, 41_000):
            d = clamped_delta(prev, cur)
            assert d >= 0
            assert d == (cur - prev if cur > prev else 0)


def test_clamp_sequence_of_polls_matches_forward_progress_only():
    """A realistic week: listen, scrub back, relisten, finish.

    Total credited must equal the forward movement only.
    """
    positions = [0, 10 * MIN, 25 * MIN, 5 * MIN, 20 * MIN, 20 * MIN, 0, 8 * MIN]
    total = sum(clamped_delta(a, b) for a, b in zip(positions, positions[1:]))
    assert total == (10 + 15 + 15 + 8) * MIN
    naive = positions[-1] - positions[0]
    assert total > naive, "the clamp must credit more than a naive end-minus-start"


def test_clamp_against_fake_spotify_positions():
    spot = fake_spotify()
    prev = spot.get_episode(PARTIAL_ID).resume_position_ms
    spot.set_position(PARTIAL_ID, prev + 9 * MIN)
    assert clamped_delta(prev, spot.get_episode(PARTIAL_ID).resume_position_ms) == 9 * MIN
    prev = spot.get_episode(PARTIAL_ID).resume_position_ms
    spot.set_position(PARTIAL_ID, 0)   # relisten
    assert clamped_delta(prev, spot.get_episode(PARTIAL_ID).resume_position_ms) == 0


# ---------------------------------------------------------------------------
# parse_episode_ref
# ---------------------------------------------------------------------------

GOOD_ID = "4rOoJ6Egrf8K2IrywzwOMk"


@pytest.mark.parametrize("ref", [
    f"https://open.spotify.com/episode/{GOOD_ID}",
    f"http://open.spotify.com/episode/{GOOD_ID}",
    f"https://open.spotify.com/episode/{GOOD_ID}?si=8a1f2b3c4d5e6f70",
    f"https://open.spotify.com/episode/{GOOD_ID}?si=abc&nd=1&dlsi=xyz",
    f"https://open.spotify.com/episode/{GOOD_ID}#t=120",
    f"https://open.spotify.com/intl-pt/episode/{GOOD_ID}",
    f"https://open.spotify.com/intl-pt/episode/{GOOD_ID}?si=abc",
    f"https://open.spotify.com/intl-de/episode/{GOOD_ID}",
    f"https://www.open.spotify.com/episode/{GOOD_ID}",
    f"open.spotify.com/episode/{GOOD_ID}",
    f"  https://open.spotify.com/episode/{GOOD_ID}?si=abc  \n",
    f"spotify:episode:{GOOD_ID}",
    f"  spotify:episode:{GOOD_ID}\n",
])
def test_parse_accepts_both_forms(ref):
    assert parse_episode_ref(ref) == GOOD_ID


def test_parse_returns_bare_22_char_base62_id():
    got = parse_episode_ref(f"https://open.spotify.com/episode/{GOOD_ID}?si=x")
    assert spotify.EPISODE_ID_RE.match(got)
    assert len(got) == 22


@pytest.mark.parametrize("ref,word", [
    (f"https://open.spotify.com/track/{GOOD_ID}", "track"),
    (f"https://open.spotify.com/show/{GOOD_ID}", "show"),
    (f"https://open.spotify.com/album/{GOOD_ID}", "album"),
    (f"https://open.spotify.com/playlist/{GOOD_ID}", "playlist"),
    (f"https://open.spotify.com/artist/{GOOD_ID}", "artist"),
    (f"https://open.spotify.com/intl-pt/track/{GOOD_ID}?si=q", "track"),
    (f"spotify:track:{GOOD_ID}", "track"),
    (f"spotify:show:{GOOD_ID}", "show"),
    (f"spotify:album:{GOOD_ID}", "album"),
    (f"spotify:playlist:{GOOD_ID}", "playlist"),
])
def test_parse_names_the_wrong_spotify_type(ref, word):
    """A track/show/album/playlist link must be told what it actually is."""
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref(ref)
    msg = str(exc.value)
    assert word in msg.lower(), msg
    assert "episode" in msg.lower(), msg


def test_parse_show_link_says_how_to_get_the_episode():
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref(f"https://open.spotify.com/show/{GOOD_ID}")
    assert "open the episode" in str(exc.value).lower()


@pytest.mark.parametrize("ref", [
    "",
    "   ",
    "\n\t ",
    "hello",
    "why did you send me this",
    "https://youtube.com/watch?v=abc",
    "https://podcasts.apple.com/episode/123",
    "https://example.com/episode/" + GOOD_ID,
    "spotify:episode:",
    "spotify:episode:tooshort",
    "spotify:episode:" + GOOD_ID + "extra",
    "spotify:",
    "spotify::",
    f"spotify:episode:{GOOD_ID}:extra",
    "https://open.spotify.com/episode/short",
    "https://open.spotify.com/episode/" + "x" * 23,
    "https://open.spotify.com/episode/has-a-hyphen-in-it-abcd",
    "https://open.spotify.com/episode/",
    "https://open.spotify.com/",
    "https://open.spotify.com/episode",
    GOOD_ID,                       # a bare id is not one of the two forms
])
def test_parse_rejects_everything_else(ref):
    with pytest.raises(InvalidEpisodeRef):
        parse_episode_ref(ref)


def test_parse_rejects_non_string():
    for bad in (None, 12345, [f"spotify:episode:{GOOD_ID}"]):
        with pytest.raises(InvalidEpisodeRef):
            parse_episode_ref(bad)          # type: ignore[arg-type]


def test_parse_error_is_a_value_error_and_user_facing():
    assert issubclass(InvalidEpisodeRef, ValueError)
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref("nope")
    msg = str(exc.value)
    assert msg and msg[0].isupper() and msg.endswith(".")
    assert "spotify:episode:" in msg           # tells the user what is accepted


def test_parse_never_silently_ignores_a_malformed_id():
    """Wrong length must be reported with the length, not quietly truncated."""
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref("https://open.spotify.com/episode/abc")
    assert "22" in str(exc.value)


def test_parse_message_for_a_non_spotify_host_names_the_host():
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref("https://pca.st/episode/abcd")
    assert "pca.st" in str(exc.value)


# ---------------------------------------------------------------------------
# parse_episode_ref: extraction from surrounding text
#
# A share that contains a perfectly good link must not be bounced. Tolerating
# a real-world share format is not the same as silently ignoring malformed
# input — everything below still rejects text with no valid episode ref in it.
# ---------------------------------------------------------------------------

URL = f"https://open.spotify.com/episode/{GOOD_ID}"
URL_SI = f"https://open.spotify.com/episode/{GOOD_ID}?si=8a1f2b3c4d5e6f70"
URI = f"spotify:episode:{GOOD_ID}"

OTHER_ID = "7g8H9i0J1k2L3m4N5o6Pq7"
OTHER_URL = f"https://open.spotify.com/episode/{OTHER_ID}"


@pytest.mark.parametrize("text", [
    f"listen to this {URL_SI}",
    f"{URL_SI} listen to this",
    f"listen to this {URL_SI} it looked good",
    f"Worth it?\n{URL_SI}",
    f"{URL}\n\nfor the flight",
    f"\n\n{URL_SI}\n\n",
    f"here you go: {URI}",
    f"{URI} — from the desktop app",
    f"multi\nline\n{URL}\nmessage",
])
def test_parse_extracts_a_link_out_of_surrounding_text(text):
    assert parse_episode_ref(text) == GOOD_ID


@pytest.mark.parametrize("text", [
    # What Spotify's iOS share sheet actually hands to Telegram.
    f"Three Hours On One Subject by The Very Long Show, on Spotify\n{URL_SI}",
    f"Listen on Spotify: {URL_SI}",
    f"Half Heard Already \u00b7 Longform Weekly\n\n{URL}",
    f"Check out this episode on Spotify {URL_SI}",
])
def test_parse_handles_the_ios_share_sheet_format(text):
    assert parse_episode_ref(text) == GOOD_ID


def test_parse_share_sheet_prefix_does_not_leak_a_malformed_uri_error():
    """'Listen on Spotify:' looks like a URI prefix. The real link still wins."""
    assert parse_episode_ref(f"Listen on Spotify: {URL_SI}") == GOOD_ID


@pytest.mark.parametrize("text", [
    f"({URL_SI})",
    f"see {URL}.",
    f"<{URL_SI}>",
    f'"{URL}"',
    f"[{URI}]",
    f"this one: {URL}, plus a thought",
])
def test_parse_tolerates_wrapping_punctuation(text):
    assert parse_episode_ref(text) == GOOD_ID


@pytest.mark.parametrize("text", [
    f"prose {URL_SI} prose",
    f"prose https://open.spotify.com/intl-pt/episode/{GOOD_ID}?si=x prose",
    f"prose http://open.spotify.com/episode/{GOOD_ID} prose",
    f"prose open.spotify.com/episode/{GOOD_ID} prose",
    f"prose https://www.open.spotify.com/episode/{GOOD_ID} prose",
    f"prose https://open.spotify.com/episode/{GOOD_ID}#t=120 prose",
])
def test_every_url_tolerance_survives_embedding(text):
    assert parse_episode_ref(text) == GOOD_ID


@pytest.mark.parametrize("text", [
    f"https://open.spotify.com/track/{OTHER_ID} but I meant {URL}",
    f"{URL} not https://open.spotify.com/track/{OTHER_ID}",
    f"spotify:show:{OTHER_ID} {URI}",
    f"{URI} spotify:playlist:{OTHER_ID}",
    f"https://open.spotify.com/album/{OTHER_ID} https://youtube.com/x {URL}",
])
def test_an_episode_ref_beats_a_wrong_type_link_in_the_same_message(text):
    """Precedence: a valid episode ref wins wherever it sits in the text."""
    assert parse_episode_ref(text) == GOOD_ID


@pytest.mark.parametrize("text", [
    f"{URL} and also {OTHER_URL}",
    f"{URI} and also spotify:episode:{OTHER_ID}",
    f"{URL_SI}\n{OTHER_URL}",
    f"{URI} {OTHER_URL}",
])
def test_two_episode_refs_take_the_first_without_erroring(text):
    assert parse_episode_ref(text) == GOOD_ID


def test_a_malformed_episode_link_does_not_block_a_later_valid_one():
    text = f"https://open.spotify.com/episode/short oops, I meant {URL}"
    assert parse_episode_ref(text) == GOOD_ID


@pytest.mark.parametrize("text", [
    GOOD_ID,
    f"listen to {GOOD_ID}",
    f"{GOOD_ID} please",
    f"the id is {GOOD_ID}",
])
def test_a_bare_id_is_still_rejected_with_or_without_prose(text):
    """Not one of the two accepted forms, and it matches ordinary words."""
    with pytest.raises(InvalidEpisodeRef):
        parse_episode_ref(text)


def test_prose_containing_a_22_character_word_is_rejected():
    word = "Antidisestablishmentar"        # 22 chars, base62, not a link
    assert len(word) == 22
    with pytest.raises(InvalidEpisodeRef):
        parse_episode_ref(f"we talked about {word} yesterday")


@pytest.mark.parametrize("text", [
    f"my favourite episode is on https://open.spotify.com/track/{GOOD_ID}",
    f"here: https://open.spotify.com/show/{GOOD_ID} enjoy",
    f"check spotify:playlist:{GOOD_ID} out",
])
def test_a_wrong_type_link_in_prose_is_still_named(text):
    """Extraction never turns a wrong-type link into silence."""
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref(text)
    assert "not a podcast episode" in str(exc.value)


def test_a_malformed_id_in_prose_is_still_reported():
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref("try https://open.spotify.com/episode/abc thanks")
    assert "22" in str(exc.value)


def test_prose_with_a_foreign_link_still_names_the_host():
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref("listen here https://pca.st/episode/abcd instead")
    assert "pca.st" in str(exc.value)


REJECTION_MESSAGES = [
    ("", "no link in it"),
    ("   ", "no link in it"),
    ("hello", "isn't a Spotify link"),
    ("why did you send me this", "isn't a Spotify link"),
    ("https://youtube.com/watch?v=abc", "youtube.com"),
    ("https://podcasts.apple.com/episode/123", "podcasts.apple.com"),
    (f"https://example.com/episode/{GOOD_ID}", "example.com"),
    (f"https://open.spotify.com/track/{GOOD_ID}", "a track"),
    (f"https://open.spotify.com/show/{GOOD_ID}", "a show"),
    (f"spotify:album:{GOOD_ID}", "an album"),
    (f"spotify:playlist:{GOOD_ID}", "a playlist"),
    ("spotify:episode:tooshort", "22"),
    ("https://open.spotify.com/episode/short", "22"),
    ("https://open.spotify.com/episode/" + "x" * 23, "22"),
    ("spotify:", "malformed"),
    ("spotify::", "malformed"),
    (f"spotify:episode:{GOOD_ID}:extra", "malformed"),
    ("https://open.spotify.com/", "doesn't point at anything"),
    ("https://open.spotify.com/episode", "doesn't point at anything"),
    (GOOD_ID, "bare episode id"),
]


@pytest.mark.parametrize("text,fragment", REJECTION_MESSAGES)
def test_every_rejection_message_is_intact_after_the_loosening(text, fragment):
    """The extractor must not have swallowed or reworded any rejection."""
    with pytest.raises(InvalidEpisodeRef) as exc:
        parse_episode_ref(text)
    msg = str(exc.value)
    assert fragment in msg, msg
    assert msg[0].isupper() and msg.endswith(".")


# ---------------------------------------------------------------------------
# Transport harness
# ---------------------------------------------------------------------------

TOKEN_URL = "https://accounts.spotify.com/api/token"


class Recorder:
    """Programmable MockTransport. Records every request; makes no sockets."""

    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def urls(self, contains: str) -> list[httpx.Request]:
        return [r for r in self.requests if contains in str(r.url)]

    @property
    def token_calls(self) -> int:
        return len(self.urls("accounts.spotify.com"))

    @property
    def api_calls(self) -> int:
        return len(self.urls("api.spotify.com"))


def build_client(handler, **settings_overrides) -> tuple[SpotifyClient, Recorder]:
    rec = Recorder(handler)
    settings = config.test_settings(**settings_overrides)
    return SpotifyClient(settings, transport=rec.transport), rec


def simple_handler(episode_body: dict | None = None, *, status: int = 200,
                   token_status: int = 200):
    body = episode_body if episode_body is not None else episode_json()

    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(token_status, json=TOKEN_JSON)
        return httpx.Response(status, json=body)

    return handler


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_access_token_refreshes_on_first_use_and_caches():
    client, rec = build_client(simple_handler())
    assert client.access_token() == "fake-access-token"
    assert client.access_token() == "fake-access-token"
    assert client.access_token() == "fake-access-token"
    assert rec.token_calls == 1, "the token must be cached in memory"


def test_refresh_uses_refresh_token_grant_and_basic_auth():
    client, rec = build_client(simple_handler())
    client.access_token()
    req = rec.urls("accounts.spotify.com")[0]
    assert req.method == "POST"
    body = req.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=test-refresh-token" in body
    assert req.headers["Authorization"].startswith("Basic ")
    import base64
    decoded = base64.b64decode(req.headers["Authorization"].split()[1]).decode()
    assert decoded == "test-client-id:test-client-secret"
    # No browser flow anywhere near this.
    assert "code" not in body and "redirect_uri" not in body


def test_expired_token_is_renewed_transparently():
    client, rec = build_client(simple_handler())
    client.access_token()
    assert rec.token_calls == 1
    client._expires_at = 0.0            # simulate the hour passing
    client.access_token()
    assert rec.token_calls == 2


def test_token_expiry_uses_expires_in_with_leeway():
    def handler(request):
        if "accounts" in str(request.url):
            return httpx.Response(200, json={**TOKEN_JSON, "expires_in": 3600})
        return httpx.Response(200, json=episode_json())

    client, _ = build_client(handler)
    before = SpotifyClient._monotonic()
    client.access_token()
    assert 3400 < client._expires_at - before <= 3545


def test_token_endpoint_failure_raises_spotify_error():
    client, _ = build_client(simple_handler(token_status=400))
    with pytest.raises(SpotifyError) as exc:
        client.access_token()
    assert "400" in str(exc.value)


def test_token_response_without_access_token_raises():
    def handler(request):
        return httpx.Response(200, json={"token_type": "Bearer"})

    client, _ = build_client(handler)
    with pytest.raises(SpotifyError):
        client.access_token()


def test_missing_refresh_token_raises_instead_of_calling_out():
    client, rec = build_client(simple_handler(), spotify_refresh_token="")
    with pytest.raises(SpotifyError) as exc:
        client.access_token()
    assert "refresh token" in str(exc.value).lower()
    assert rec.requests == []


def test_bearer_token_is_sent_on_api_calls():
    client, rec = build_client(simple_handler())
    client.get_episode(SHORT_ID)
    api = rec.urls("api.spotify.com")[0]
    assert api.headers["Authorization"] == "Bearer fake-access-token"


def test_401_triggers_exactly_one_forced_refresh_and_retry():
    state = {"api": 0}

    def handler(request):
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_JSON)
        state["api"] += 1
        if state["api"] == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json=episode_json(SHORT_ID))

    client, rec = build_client(handler)
    ep = client.get_episode(SHORT_ID)
    assert ep.spotify_id == SHORT_ID
    assert state["api"] == 2, "exactly one retry"
    assert rec.token_calls == 2, "the retry must force a fresh token"


def test_401_twice_raises_and_does_not_retry_again():
    state = {"api": 0}

    def handler(request):
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_JSON)
        state["api"] += 1
        return httpx.Response(401, json={"error": {"message": "no"}})

    client, _ = build_client(handler)
    with pytest.raises(SpotifyError) as exc:
        client.get_episode(SHORT_ID)
    assert state["api"] == 2, "one retry only, then give up"
    assert "401" in str(exc.value)


# ---------------------------------------------------------------------------
# get_episode
# ---------------------------------------------------------------------------

def test_get_episode_maps_every_field():
    body = episode_json(
        MEDIUM_ID, name="The Interview", show_name="Longform Weekly",
        description="Plain words.", release_date="2026-08-18",
        duration_ms=43 * MIN, resume_position_ms=7 * MIN, fully_played=False,
    )
    client, rec = build_client(simple_handler(body))
    ep = client.get_episode(MEDIUM_ID)
    assert ep == Episode(
        spotify_id=MEDIUM_ID, title="The Interview", show="Longform Weekly",
        description="Plain words.", release_date="2026-08-18",
        duration_ms=43 * MIN, resume_position_ms=7 * MIN, fully_played=False,
    )
    assert f"/v1/episodes/{MEDIUM_ID}" in str(rec.urls("api.spotify.com")[0].url)


def test_get_episode_reads_fully_played_from_resume_point():
    body = episode_json(FINISHED_ID, resume_position_ms=30 * MIN, fully_played=True)
    client, _ = build_client(simple_handler(body))
    ep = client.get_episode(FINISHED_ID)
    assert ep.fully_played is True
    assert ep.resume_position_ms == 30 * MIN


def test_missing_resume_point_raises_rather_than_defaulting_to_zero():
    """The scope was lost. Zero here is indistinguishable from a real result."""
    body = episode_json(SHORT_ID, resume_point=None)
    assert "resume_point" not in body
    client, _ = build_client(simple_handler(body))
    with pytest.raises(SpotifyError) as exc:
        client.get_episode(SHORT_ID)
    assert "user-read-playback-position" in str(exc.value)


def test_null_resume_point_raises():
    body = episode_json(SHORT_ID)
    body["resume_point"] = None
    client, _ = build_client(simple_handler(body))
    with pytest.raises(SpotifyError):
        client.get_episode(SHORT_ID)


@pytest.mark.parametrize("resume", [
    {},
    {"fully_played": False},
    {"resume_position_ms": 1000},
])
def test_incomplete_resume_point_raises(resume):
    body = episode_json(SHORT_ID)
    body["resume_point"] = resume
    client, _ = build_client(simple_handler(body))
    with pytest.raises(SpotifyError):
        client.get_episode(SHORT_ID)


def test_episode_without_id_raises():
    body = episode_json(SHORT_ID)
    del body["id"]
    client, _ = build_client(simple_handler(body))
    with pytest.raises(SpotifyError):
        client.get_episode(SHORT_ID)


@pytest.mark.parametrize("status", [400, 403, 404, 500, 502])
def test_http_errors_raise_spotify_error(status):
    client, _ = build_client(simple_handler(status=status))
    with pytest.raises(SpotifyError) as exc:
        client.get_episode(SHORT_ID)
    assert str(status) in str(exc.value)


def test_rate_limit_raises_with_retry_after():
    def handler(request):
        if "accounts" in str(request.url):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(429, json={}, headers={"Retry-After": "17"})

    client, _ = build_client(handler)
    with pytest.raises(SpotifyError) as exc:
        client.get_episode(SHORT_ID)
    assert "429" in str(exc.value) and "17" in str(exc.value)


def test_non_json_response_raises():
    def handler(request):
        if "accounts" in str(request.url):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(200, text="<html>nope</html>")

    client, _ = build_client(handler)
    with pytest.raises(SpotifyError):
        client.get_episode(SHORT_ID)


def test_transport_error_becomes_spotify_error():
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    client, _ = build_client(handler)
    with pytest.raises(SpotifyError):
        client.get_episode(SHORT_ID)


# ---------------------------------------------------------------------------
# Description HTML stripping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("<p>Hello there.</p>", "Hello there."),
    ("Plain text, no markup.", "Plain text, no markup."),
    ("A &amp; B &lt;3 &quot;quoted&quot;", 'A & B <3 "quoted"'),
    ('Visit <a href="https://x.test">the site</a> now.', "Visit the site now."),
    ("one<br/>two", "one\ntwo"),
    ("<p>one</p><p>two</p>", "one\n\ntwo"),
    ("<strong>bold</strong> and <em>italic</em>", "bold and italic"),
    ("   padded   ", "padded"),
    ("<ul><li>a</li><li>b</li></ul>", "a\nb"),
])
def test_strip_html(raw, expected):
    assert spotify.strip_html(raw) == expected


def test_description_is_html_stripped_on_the_way_out():
    body = episode_json(
        SHORT_ID,
        description='<p>We talk to <a href="https://x.test">someone</a>.'
                    "<br>Sponsored by Nobody &amp; Co.</p>",
    )
    client, _ = build_client(simple_handler(body))
    ep = client.get_episode(SHORT_ID)
    assert "<" not in (ep.description or "")
    assert "&amp;" not in (ep.description or "")
    assert "Nobody & Co." in (ep.description or "")


def test_none_description_stays_none():
    assert spotify.strip_html(None) is None
    body = episode_json(SHORT_ID, description=None)
    client, _ = build_client(simple_handler(body))
    assert client.get_episode(SHORT_ID).description is None


def test_missing_show_object_does_not_crash():
    body = episode_json(SHORT_ID, show_name=None)
    client, _ = build_client(simple_handler(body))
    assert client.get_episode(SHORT_ID).show == ""


# ---------------------------------------------------------------------------
# get_episodes: batching at 50, null tolerance
# ---------------------------------------------------------------------------

def batch_handler(known: set[str] | None = None):
    """Returns the /episodes array with a null for anything unknown."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_JSON)
        ids = request.url.params.get("ids", "").split(",")
        items = [
            None if (known is not None and i not in known) else episode_json(i)
            for i in ids if i
        ]
        return httpx.Response(200, json={"episodes": items})

    return handler


@pytest.mark.parametrize("n,expected_batches,expected_sizes", [
    (1, 1, [1]),
    (49, 1, [49]),
    (50, 1, [50]),
    (51, 2, [50, 1]),
    (100, 2, [50, 50]),
    (120, 3, [50, 50, 20]),
])
def test_get_episodes_batches_at_fifty(n, expected_batches, expected_sizes):
    ids = make_ids(n)
    client, rec = build_client(batch_handler())
    eps = client.get_episodes(ids)
    api = rec.urls("api.spotify.com")
    assert len(api) == expected_batches
    sizes = [len(r.url.params["ids"].split(",")) for r in api]
    assert sizes == expected_sizes
    assert max(sizes) <= 50
    assert [e.spotify_id for e in eps] == ids


def test_get_episodes_tolerates_nulls_in_the_array():
    ids = [SHORT_ID, UNKNOWN_ID, MEDIUM_ID]
    client, _ = build_client(batch_handler(known={SHORT_ID, MEDIUM_ID}))
    eps = client.get_episodes(ids)
    assert [e.spotify_id for e in eps] == [SHORT_ID, MEDIUM_ID]


def test_get_episodes_all_nulls_returns_empty_list():
    client, _ = build_client(batch_handler(known=set()))
    assert client.get_episodes([SHORT_ID, MEDIUM_ID]) == []


def test_get_episodes_empty_input_makes_no_request():
    client, rec = build_client(batch_handler())
    assert client.get_episodes([]) == []
    assert rec.requests == []


def test_get_episodes_preserves_order():
    ids = [LONG_ID, SHORT_ID, PARTIAL_ID, MEDIUM_ID]
    client, _ = build_client(batch_handler())
    assert [e.spotify_id for e in client.get_episodes(ids)] == ids


def test_get_episodes_reuses_one_token_across_batches():
    client, rec = build_client(batch_handler())
    client.get_episodes(make_ids(120))
    assert rec.token_calls == 1
    assert rec.api_calls == 3


def test_get_episodes_missing_array_raises():
    def handler(request):
        if "accounts" in str(request.url):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(200, json={"nope": []})

    client, _ = build_client(handler)
    with pytest.raises(SpotifyError):
        client.get_episodes([SHORT_ID])


def test_get_episodes_propagates_missing_resume_point():
    def handler(request):
        if "accounts" in str(request.url):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(200, json={
            "episodes": [episode_json(SHORT_ID, resume_point=None)]})

    client, _ = build_client(handler)
    with pytest.raises(SpotifyError) as exc:
        client.get_episodes([SHORT_ID])
    assert "user-read-playback-position" in str(exc.value)


# ---------------------------------------------------------------------------
# FakeSpotify
# ---------------------------------------------------------------------------

def test_fake_has_the_same_surface_as_the_real_client():
    import inspect
    for name in ("access_token", "get_episode", "get_episodes"):
        real = inspect.signature(getattr(SpotifyClient, name))
        fake = inspect.signature(getattr(FakeSpotify, name))
        assert real == fake, name
    assert hasattr(FakeSpotify, "set_position")


def test_fake_get_episode_returns_the_fixture():
    spot = fake_spotify()
    ep = spot.get_episode(MEDIUM_ID)
    assert ep is EPISODES[MEDIUM_ID]
    assert ep.duration_ms == 43 * MIN


def test_fake_get_episode_unknown_raises_spotify_error():
    with pytest.raises(SpotifyError):
        fake_spotify().get_episode(UNKNOWN_ID)


def test_fake_set_position_moves_the_resume_point():
    spot = fake_spotify()
    spot.set_position(SHORT_ID, 4 * MIN)
    ep = spot.get_episode(SHORT_ID)
    assert ep.resume_position_ms == 4 * MIN
    assert ep.fully_played is False
    assert ep.title == EPISODES[SHORT_ID].title, "only the position changes"


def test_fake_set_position_can_mark_fully_played():
    spot = fake_spotify()
    spot.set_position(MEDIUM_ID, 42 * MIN, fully_played=True)
    assert spot.get_episode(MEDIUM_ID).fully_played is True


def test_fake_set_position_does_not_mutate_the_shared_fixture():
    spot = fake_spotify()
    spot.set_position(SHORT_ID, 99 * MIN)
    assert EPISODES[SHORT_ID].resume_position_ms == 0
    assert fake_spotify().get_episode(SHORT_ID).resume_position_ms == 0


def test_fake_set_position_unknown_id_raises():
    with pytest.raises(KeyError):
        fake_spotify().set_position(UNKNOWN_ID, 1000)


def test_fake_get_episodes_skips_unknown_ids():
    spot = fake_spotify()
    eps = spot.get_episodes([SHORT_ID, UNKNOWN_ID, LONG_ID])
    assert [e.spotify_id for e in eps] == [SHORT_ID, LONG_ID]


def test_fake_get_episodes_empty_and_records_calls():
    spot = fake_spotify()
    assert spot.get_episodes([]) == []
    spot.get_episode(SHORT_ID)
    assert ("get_episode", SHORT_ID) in spot.calls


def test_fake_access_token_needs_no_network():
    assert fake_spotify().access_token() == "fake-access-token"


def test_fake_accepts_a_custom_dict():
    custom = make_episode("7g8H9i0J1k2L3m4N5o6Pq7", title="Custom")
    spot = FakeSpotify({custom.spotify_id: custom})
    assert spot.get_episode(custom.spotify_id).title == "Custom"
    with pytest.raises(SpotifyError):
        spot.get_episode(SHORT_ID)


def test_fixtures_are_structurally_valid_episodes():
    for sid, ep in EPISODES.items():
        assert spotify.EPISODE_ID_RE.match(sid), sid
        assert ep.spotify_id == sid
        assert isinstance(ep, Episode)
        assert ep.duration_ms > 0
        assert 0 <= ep.resume_position_ms <= ep.duration_ms
    assert spotify.EPISODE_ID_RE.match(UNKNOWN_ID)
    assert UNKNOWN_ID not in EPISODES


def test_episode_is_frozen():
    with pytest.raises(Exception):
        EPISODES[SHORT_ID].title = "no"      # type: ignore[misc]


# ---------------------------------------------------------------------------
# The batch-403 fallback.
#
# Observed against the live API on 2026-08-30: GET /episodes/{id} returns 200
# while GET /episodes?ids=... returns 403 for the SAME token and the SAME
# episode, with or without `market`. A Spotify-side restriction on the
# several-episodes endpoint. The brief already budgets for the fallback:
# "Cost is one API call per queued episode."
# ---------------------------------------------------------------------------

def _batch_403_handler(ids_to_bodies: dict[str, dict]):
    """403 on the batch endpoint, 200 on each single-episode endpoint."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "accounts.spotify.com" in url:
            return httpx.Response(200, json=TOKEN_JSON)
        if "/v1/episodes?" in url or url.endswith("/v1/episodes"):
            return httpx.Response(403, json={"error": {"status": 403,
                                                       "message": "Forbidden"}})
        eid = url.split("/v1/episodes/")[1].split("?")[0]
        if eid in ids_to_bodies:
            return httpx.Response(200, json=ids_to_bodies[eid])
        return httpx.Response(404, json={"error": {"status": 404,
                                                   "message": "Not found"}})
    return handler


def test_403_on_the_batch_endpoint_falls_back_to_single_fetches():
    ids = make_ids(3)
    bodies = {i: episode_json(spotify_id=i) for i in ids}
    client, rec = build_client(_batch_403_handler(bodies))

    episodes = client.get_episodes(ids)

    assert [e.spotify_id for e in episodes] == ids
    assert len(rec.urls("/v1/episodes/")) == 3, "one request per episode"


def test_the_fallback_is_latched_so_the_batch_is_tried_only_once():
    """A queue of 30 episodes must cost one wasted request, not thirty."""
    ids = make_ids(4)
    bodies = {i: episode_json(spotify_id=i) for i in ids}
    client, rec = build_client(_batch_403_handler(bodies))

    client.get_episodes(ids[:2])
    batch_attempts_after_first = len(
        [r for r in rec.requests if "/v1/episodes?" in str(r.url)]
    )
    client.get_episodes(ids[2:])
    batch_attempts_after_second = len(
        [r for r in rec.requests if "/v1/episodes?" in str(r.url)]
    )

    assert batch_attempts_after_first == 1
    assert batch_attempts_after_second == 1, "batch must not be retried once latched"


def test_the_fallback_skips_an_unavailable_id_rather_than_failing_the_poll():
    """Mirrors the batch contract: nulls are skipped, so callers key by id."""
    ids = make_ids(3)
    bodies = {ids[0]: episode_json(spotify_id=ids[0]),
              ids[2]: episode_json(spotify_id=ids[2])}   # ids[1] 404s
    client, _ = build_client(_batch_403_handler(bodies))

    episodes = client.get_episodes(ids)

    assert [e.spotify_id for e in episodes] == [ids[0], ids[2]]
    assert len(episodes) < len(ids), "shorter than requested — never zip by index"


def test_the_fallback_still_refuses_to_swallow_a_lost_scope():
    """A missing resume_point must escape the per-id error tolerance."""
    ids = make_ids(2)
    no_resume = episode_json(spotify_id=ids[0])
    no_resume.pop("resume_point", None)
    bodies = {ids[0]: no_resume, ids[1]: episode_json(spotify_id=ids[1])}
    client, _ = build_client(_batch_403_handler(bodies))

    with pytest.raises(spotify.SpotifyScopeError, match="user-read-playback-position"):
        client.get_episodes(ids)


def test_forbidden_and_scope_errors_are_spotify_errors():
    """Existing `except SpotifyError` handlers must keep catching both."""
    assert issubclass(spotify.SpotifyForbidden, spotify.SpotifyError)
    assert issubclass(spotify.SpotifyScopeError, spotify.SpotifyError)


def test_a_403_on_a_single_episode_still_raises():
    """Only the batch endpoint gets the fallback; a real 403 must surface."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "accounts.spotify.com" in str(request.url):
            return httpx.Response(200, json=TOKEN_JSON)
        return httpx.Response(403, json={"error": {"status": 403,
                                                   "message": "Forbidden"}})
    client, _ = build_client(handler)
    with pytest.raises(spotify.SpotifyForbidden):
        client.get_episode(make_ids(1)[0])
