"""Tests for the X API v2 timeline poller (mocked requests; no network).

Defended here:

* **Off by default.** Without ``X_FEED_ENABLED``, ``connect()`` returns False
  and NOTHING touches the network — the pay-per-use meter cannot start by
  accident.
* **Incremental polling.** ``since_id`` is threaded per handle so every post
  is billed at most once, and the >=60s per-handle floor blocks a poll inside
  the window without a request.
* **The tape.** New posts append to ``x_posts_<UTCdate>.jsonl`` as raw JSON
  lines; API failures degrade to gaps, never to a crash.
"""

import json
import os
import sys
from datetime import datetime, timezone

import pytest
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data.x_provider import XProvider


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    """Queue of canned responses; records every request made."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


USERS_BY = _FakeResponse(
    {"data": [{"id": "111", "username": "alice"}, {"id": "222", "username": "bob"}]}
)


def _timeline(posts, newest_id=None):
    body = {"data": posts}
    if newest_id is not None:
        body["meta"] = {"newest_id": newest_id, "result_count": len(posts)}
    return _FakeResponse(body)


def _enabled(monkeypatch):
    monkeypatch.setenv("X_FEED_ENABLED", "1")
    monkeypatch.setenv("X_BEARER_TOKEN", "test-bearer")
    monkeypatch.delenv("X_TRACK_HANDLES", raising=False)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_disabled_by_default_makes_no_requests(monkeypatch, tmp_path):
    monkeypatch.delenv("X_FEED_ENABLED", raising=False)
    monkeypatch.setenv("X_BEARER_TOKEN", "test-bearer")
    session = _FakeSession()
    provider = XProvider(handles=["alice"], feed_dir=tmp_path, session=session)

    assert provider.enabled is False
    assert provider.connect() is False
    assert provider.fetch_latest("alice") is None
    assert provider.poll_handle("alice") == []
    assert session.calls == []
    assert list(tmp_path.iterdir()) == []


def test_enabled_but_missing_token_or_handles_stays_off(monkeypatch, tmp_path):
    monkeypatch.setenv("X_FEED_ENABLED", "true")
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    session = _FakeSession()
    provider = XProvider(handles=["alice"], feed_dir=tmp_path, session=session)
    assert provider.connect() is False

    monkeypatch.setenv("X_BEARER_TOKEN", "test-bearer")
    provider = XProvider(handles=[], feed_dir=tmp_path, session=session)
    assert provider.connect() is False
    assert session.calls == []


def test_handles_parsed_from_env_csv(monkeypatch, tmp_path):
    _enabled(monkeypatch)
    monkeypatch.setenv("X_TRACK_HANDLES", " @alice, bob ,,")
    provider = XProvider(feed_dir=tmp_path, session=_FakeSession())
    assert provider.handles == ["alice", "bob"]


# --------------------------------------------------------------------------
# Connect
# --------------------------------------------------------------------------


def test_connect_resolves_user_ids_with_bearer_auth(monkeypatch, tmp_path):
    _enabled(monkeypatch)
    session = _FakeSession([USERS_BY])
    provider = XProvider(handles=["alice", "bob"], feed_dir=tmp_path, session=session)

    assert provider.connect() is True
    assert provider._user_ids == {"alice": "111", "bob": "222"}
    call = session.calls[0]
    assert call["url"].endswith("/users/by")
    assert call["params"] == {"usernames": "alice,bob"}
    assert call["headers"]["Authorization"] == "Bearer test-bearer"


def test_connect_failure_is_a_clean_false(monkeypatch, tmp_path):
    _enabled(monkeypatch)
    session = _FakeSession([_FakeResponse({}, status=500)])
    provider = XProvider(handles=["alice"], feed_dir=tmp_path, session=session)
    assert provider.connect() is False
    assert provider.connected is False


# --------------------------------------------------------------------------
# Polling + the tape
# --------------------------------------------------------------------------


def _connected_provider(monkeypatch, tmp_path, timeline_responses):
    _enabled(monkeypatch)
    session = _FakeSession([USERS_BY] + list(timeline_responses))
    provider = XProvider(handles=["alice", "bob"], feed_dir=tmp_path, session=session)
    assert provider.connect() is True
    return provider, session


def test_poll_appends_raw_posts_to_the_daily_tape(monkeypatch, tmp_path):
    posts = [
        {"id": "902", "text": "second post", "created_at": "2026-09-01T12:05:00Z"},
        {"id": "901", "text": "first post", "created_at": "2026-09-01T12:00:00Z"},
    ]
    provider, session = _connected_provider(
        monkeypatch, tmp_path, [_timeline(posts, newest_id="902")]
    )

    returned = provider.poll_handle("alice")
    assert [p["id"] for p in returned] == ["902", "901"]

    tape = tmp_path / f"x_posts_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    assert tape.exists()
    lines = [json.loads(l) for l in tape.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["handle"] == "alice"
    assert lines[0]["post"]["id"] == "902"
    assert "fetched_at" in lines[0]

    call = session.calls[-1]
    assert call["url"].endswith("/users/111/tweets")
    assert "since_id" not in call["params"]  # first poll has no watermark
    assert call["params"]["tweet.fields"] == "created_at,referenced_tweets"


def test_since_id_is_threaded_on_the_next_poll(monkeypatch, tmp_path):
    provider, session = _connected_provider(
        monkeypatch,
        tmp_path,
        [
            _timeline([{"id": "902", "text": "a"}], newest_id="902"),
            _timeline([], newest_id=None),
        ],
    )

    provider.poll_handle("alice")
    assert provider._since_ids["alice"] == "902"

    # Step past the interval floor, then poll again: since_id must be sent.
    provider._last_poll["alice"] -= provider.MIN_POLL_INTERVAL_S + 1
    assert provider.poll_handle("alice") == []
    assert session.calls[-1]["params"]["since_id"] == "902"


def test_poll_inside_the_interval_floor_makes_no_request(monkeypatch, tmp_path):
    provider, session = _connected_provider(
        monkeypatch, tmp_path, [_timeline([{"id": "1", "text": "x"}], newest_id="1")]
    )

    provider.poll_handle("alice")
    n_calls = len(session.calls)
    # Immediately again: inside the 60s window, so no request and no error.
    assert provider.poll_handle("alice") == []
    assert len(session.calls) == n_calls


def test_poll_failure_returns_empty_and_keeps_the_watermark(monkeypatch, tmp_path):
    provider, session = _connected_provider(
        monkeypatch,
        tmp_path,
        [
            _timeline([{"id": "902", "text": "a"}], newest_id="902"),
            _FakeResponse({}, status=429),
        ],
    )
    provider.poll_handle("alice")
    provider._last_poll["alice"] -= provider.MIN_POLL_INTERVAL_S + 1
    assert provider.poll_handle("alice") == []
    assert provider._since_ids["alice"] == "902"


def test_untracked_handle_polls_nothing(monkeypatch, tmp_path):
    provider, session = _connected_provider(monkeypatch, tmp_path, [])
    n_calls = len(session.calls)
    assert provider.poll_handle("mallory") == []
    assert len(session.calls) == n_calls


# --------------------------------------------------------------------------
# fetch_latest (DataProvider surface)
# --------------------------------------------------------------------------


def test_fetch_latest_returns_market_data_with_the_newest_post(
    monkeypatch, tmp_path
):
    posts = [{"id": "902", "text": "hello", "created_at": "2026-09-01T12:05:00Z"}]
    provider, _ = _connected_provider(
        monkeypatch, tmp_path, [_timeline(posts, newest_id="902")]
    )

    data = provider.fetch_latest("alice")
    assert data is not None
    assert data.symbol == "alice"
    assert data.price == 0.0  # text feed, not a quote feed
    assert data.extra["post_id"] == "902"
    assert data.extra["post_text"] == "hello"
    assert data.extra["new_posts_this_poll"] == 1
    assert data.extra["source"] == "x_api_v2"


def test_fetch_latest_is_none_before_any_post_is_seen(monkeypatch, tmp_path):
    provider, _ = _connected_provider(monkeypatch, tmp_path, [_timeline([])])
    assert provider.fetch_latest("alice") is None
    assert provider.fetch_latest("mallory") is None
