"""Reusable Spotify fixtures.

Owned by Agent B, imported by anyone who needs a `FakeSpotify` or a plausible
`Episode`. Nothing here touches the network or the filesystem.

Usage::

    from tests.fixtures_spotify import EPISODES, fake_spotify, make_episode

    spot = fake_spotify()
    spot.set_position(SHORT_ID, 120_000)

Durations are chosen to exercise the 180-minute weekly allowance: LONG alone
blows a week, SHORT and MEDIUM together fit comfortably.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spotify import Episode, FakeSpotify  # noqa: E402

__all__ = [
    "SHORT_ID", "MEDIUM_ID", "LONG_ID", "PARTIAL_ID", "FINISHED_ID",
    "UNKNOWN_ID", "ALL_IDS",
    "SHORT", "MEDIUM", "LONG", "PARTIAL", "FINISHED",
    "EPISODES", "make_episode", "fake_spotify",
    "episode_json", "TOKEN_JSON", "make_ids",
]

MIN = 60_000

SHORT_ID = "1a2B3c4D5e6F7g8H9i0Jk1"     # 12 min
MEDIUM_ID = "2b3C4d5E6f7G8h9I0j1Kl2"    # 43 min
LONG_ID = "3c4D5e6F7g8H9i0J1k2Lm3"      # 2h 47m — one of these eats a week
PARTIAL_ID = "4rOoJ6Egrf8K2IrywzwOMk"   # 58 min, 21 min already heard
FINISHED_ID = "5e6F7g8H9i0J1k2L3m4No5"  # 31 min, fully_played
UNKNOWN_ID = "6f7G8h9I0j1K2l3M4n5Op6"   # deliberately NOT in EPISODES

ALL_IDS = [SHORT_ID, MEDIUM_ID, LONG_ID, PARTIAL_ID, FINISHED_ID]


def make_episode(
    spotify_id: str = SHORT_ID,
    *,
    title: str = "An Episode",
    show: str = "A Show",
    description: str | None = "Plain description.",
    release_date: str | None = "2026-08-01",
    duration_ms: int = 12 * MIN,
    resume_position_ms: int = 0,
    fully_played: bool = False,
) -> Episode:
    """Factory with sane defaults; override only what the test cares about."""
    return Episode(
        spotify_id=spotify_id,
        title=title,
        show=show,
        description=description,
        release_date=release_date,
        duration_ms=duration_ms,
        resume_position_ms=resume_position_ms,
        fully_played=fully_played,
    )


SHORT = make_episode(
    SHORT_ID,
    title="A Twelve Minute Thing",
    show="Short Cuts",
    description="Twelve minutes. Cheap against the allowance.",
    release_date="2026-08-20",
    duration_ms=12 * MIN,
)

MEDIUM = make_episode(
    MEDIUM_ID,
    title="The Forty Three Minute Interview",
    show="Longform Weekly",
    description="A conversation about deliberate consumption.",
    release_date="2026-08-18",
    duration_ms=43 * MIN,
)

LONG = make_episode(
    LONG_ID,
    title="Three Hours On One Subject",
    show="The Very Long Show",
    description="167 minutes. Two thirds of a 180 minute week in one decision.",
    release_date="2026-08-11",
    duration_ms=167 * MIN,
)

PARTIAL = make_episode(
    PARTIAL_ID,
    title="Half Heard Already",
    show="Longform Weekly",
    description="Started on the bike, never finished.",
    release_date="2026-07-30",
    duration_ms=58 * MIN,
    resume_position_ms=21 * MIN,
)

FINISHED = make_episode(
    FINISHED_ID,
    title="Actually Listened To",
    show="Short Cuts",
    description="The success outcome.",
    release_date="2026-07-12",
    duration_ms=31 * MIN,
    resume_position_ms=30 * MIN,   # trailing credits: never reaches 100%
    fully_played=True,
)

EPISODES: dict[str, Episode] = {
    e.spotify_id: e for e in (SHORT, MEDIUM, LONG, PARTIAL, FINISHED)
}


def fake_spotify(episodes: dict[str, Episode] | None = None,
                 **extra: Episode) -> FakeSpotify:
    """A FakeSpotify preloaded with the standard fixtures."""
    base = dict(EPISODES if episodes is None else episodes)
    base.update({ep.spotify_id: ep for ep in extra.values()})
    return FakeSpotify(base)


# --- raw API shapes, for transport-level tests ------------------------------

TOKEN_JSON = {
    "access_token": "fake-access-token",
    "token_type": "Bearer",
    "expires_in": 3600,
    "scope": "user-read-playback-position",
}


def episode_json(
    spotify_id: str = SHORT_ID,
    *,
    name: str = "An Episode",
    show_name: str | None = "A Show",
    description: str | None = "Plain description.",
    release_date: str | None = "2026-08-01",
    duration_ms: int = 12 * MIN,
    resume_position_ms: int | None = 0,
    fully_played: bool | None = False,
    resume_point: object = ...,
) -> dict:
    """A Spotify episode object as the API returns it.

    Pass ``resume_point=None`` (or any other value) to simulate the token
    having lost the ``user-read-playback-position`` scope.
    """
    body: dict = {
        "id": spotify_id,
        "type": "episode",
        "name": name,
        "description": description,
        "release_date": release_date,
        "duration_ms": duration_ms,
        "uri": f"spotify:episode:{spotify_id}",
    }
    if show_name is not None:
        body["show"] = {"id": "show" + spotify_id[:18], "name": show_name}
    if resume_point is ...:
        body["resume_point"] = {
            "resume_position_ms": resume_position_ms,
            "fully_played": fully_played,
        }
    elif resume_point is not None:
        body["resume_point"] = resume_point
    return body


def make_ids(n: int, prefix: str = "z") -> list[str]:
    """`n` distinct, structurally valid 22-char base62 episode ids."""
    return [(prefix + f"{i:021d}")[:22] for i in range(n)]
