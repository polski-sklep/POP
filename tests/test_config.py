"""Tests for config.py — the timezone setting in particular.

`Settings.tz` decides where the Sunday 18:00 triage boundary falls, and that
boundary defines the week: the allowance window, the debt calculation, every
deadline comparison. An hour of drift moves all of it.

D1.3 protects that boundary in the arithmetic — `clock.next_week_start` is
computed in local time so a DST change shifts the UTC instant correctly. D5.2
protects the same boundary in the config layer, which is where it used to be
reachable: `TZ` is the standard POSIX *system* timezone variable, and while
`config.load()` overlaid it, any cron or systemd unit exporting `TZ=UTC` would
have silently moved triage an hour for half the year. The setting is `POP_TZ`
and nothing named `TZ` reaches `Settings`.

No test here reads the real `.env`; the root conftest raises if one tries.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config  # noqa: E402


@pytest.fixture()
def env_file(tmp_path):
    """Writes a throwaway .env and hands back its path."""
    def write(**pairs) -> pathlib.Path:
        path = tmp_path / "dotenv"
        path.write_text("".join(f"{k}={v}\n" for k, v in pairs.items()))
        return path
    return write


# ==========================================================================
# POP_TZ is the setting
# ==========================================================================

def test_pop_tz_is_read_from_the_env_file(env_file):
    settings = config.load(env_file(POP_TZ="Atlantic/Azores"))
    assert settings.tz == "Atlantic/Azores"


def test_pop_tz_defaults_to_lisbon_when_nothing_sets_it(env_file):
    assert config.load(env_file()).tz == "Europe/Lisbon"


def test_pop_tz_from_the_environment_overrides_the_env_file(env_file, monkeypatch):
    """Deployment override, the same as every other key in `_KEYS`."""
    monkeypatch.setenv("POP_TZ", "Atlantic/Azores")
    assert config.load(env_file(POP_TZ="Europe/Lisbon")).tz == "Atlantic/Azores"


def test_an_explicit_override_still_wins(env_file, monkeypatch):
    monkeypatch.setenv("POP_TZ", "Atlantic/Azores")
    assert config.load(env_file(), pop_tz="Europe/Madrid").tz == "Europe/Madrid"


# ==========================================================================
# The POSIX TZ variable must not reach Settings — D5.2
# ==========================================================================

def test_an_ambient_posix_tz_does_not_change_settings_tz(env_file, monkeypatch):
    """The regression. `TZ=UTC` in a cron environment moved triage an hour."""
    monkeypatch.setenv("TZ", "UTC")
    assert config.load(env_file(POP_TZ="Europe/Lisbon")).tz == "Europe/Lisbon"


def test_an_ambient_posix_tz_cannot_supply_the_default_either(env_file, monkeypatch):
    """With nothing configured, `TZ` must not be the fallback — Lisbon is."""
    monkeypatch.setenv("TZ", "UTC")
    assert config.load(env_file()).tz == "Europe/Lisbon"


def test_a_tz_line_in_the_env_file_is_ignored(env_file, monkeypatch):
    """An old .env carrying the pre-rename key falls back to the default.

    It does not silently keep working under the POSIX name, which is the point
    of the rename: `check_env.py` fails such a file loudly instead.
    """
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.delenv("POP_TZ", raising=False)
    assert config.load(env_file(TZ="UTC")).tz == "Europe/Lisbon"


def test_tz_is_not_in_the_environment_override_key_set():
    """The structural guarantee, so no future key list quietly re-adds it."""
    assert "TZ" not in config._KEYS
    assert "POP_TZ" in config._KEYS


def test_check_env_validates_pop_tz_and_not_tz():
    """The Phase 0 gate has to ask for the key the app actually reads."""
    import check_env

    assert "POP_TZ" in check_env.REQUIRED
    assert "TZ" not in check_env.REQUIRED


# ==========================================================================
# Nothing above disturbed the rest of the settings
# ==========================================================================

def test_the_other_settings_still_load(env_file):
    settings = config.load(env_file(
        TELEGRAM_USER_ID="12345",
        WEEKLY_ALLOWANCE_MIN="90",
        LOCK_HOURS="24",
        LIFESPAN_CYCLES="3",
    ))
    assert settings.telegram_user_id == 12345
    assert settings.weekly_allowance_min == 90
    assert settings.lock_hours == 24
    assert settings.lifespan_cycles == 3
    assert settings.lock_seconds == 24 * 3600


def test_test_settings_is_unaffected_by_the_environment(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("POP_TZ", "UTC")
    assert config.test_settings().tz == "Europe/Lisbon"
