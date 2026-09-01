"""X (Twitter) API v2 timeline poller — feed-only, disabled by default.

A :class:`src.core.interfaces.DataProvider` that polls the official X API v2
user-timeline endpoint for a small set of tracked handles and appends every
raw post to ``data/x_feed/x_posts_<UTCdate>.jsonl``. It exists to build a
transcript tape for the mention engine (see
:mod:`src.strategies.mention_strategy`), nothing else: NOTHING in the runtime
constructs it yet — no orchestrator wiring — it is a ready component.

GATING
------
``X_FEED_ENABLED`` (env, default off): when off, :meth:`connect` returns
``False`` and nothing ever polls or spends. When on, ``X_BEARER_TOKEN``
supplies the pay-per-use bearer token and ``X_TRACK_HANDLES`` (comma-separated
usernames) the watchlist.

COST MODEL (why since_id and the 60s floor are load-bearing)
------------------------------------------------------------
The pay-per-use API bills ~$0.005 per **unique** post read, with a 24h dedup
window — re-reading the same post within 24h is free, so the marginal cost of
a poll is only the posts that are actually new. Tracking ``since_id`` per
handle keeps every poll incremental, and the >=60s per-handle interval floor
keeps the request count itself polite. For ~5 handles of normal posting volume
this works out to roughly $10-25/month.

WHY THIS SOURCE IS SETTLEMENT-GRADE
-----------------------------------
Kalshi's own ``KXELONTWEETS`` settlement is, per the CFTC-certified TWEETS
rulebook, itself a 5-minute X API poller — so polling the same API is reading
the same instrument the exchange settles on, not a proxy for it.

Uses ``requests`` plus the stdlib only (no new dependencies).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from src.core.interfaces import DataProvider, MarketData
from src.utils.logger import logger

#: Default location of the raw-post tape (one file per UTC day).
DEFAULT_FEED_DIR = Path("data") / "x_feed"

_TRUTHY = ("1", "true", "yes", "on")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _parse_handles(raw: str) -> List[str]:
    return [h.strip().lstrip("@") for h in (raw or "").split(",") if h.strip()]


class XProvider(DataProvider):
    """Polls tracked X handles' timelines; appends raw posts to a JSONL tape."""

    API_BASE = "https://api.x.com/2"
    #: Per-handle floor between timeline requests. The cost model above is why
    #: this is a hard floor, not a default.
    MIN_POLL_INTERVAL_S = 60.0
    #: Timeline page size (API maximum). One page per poll: a handle that
    #: outruns 100 posts per interval is not a handle this cost model tracks.
    MAX_RESULTS_PER_POLL = 100
    TIMEOUT_S = 10

    def __init__(
        self,
        handles: Optional[List[str]] = None,
        feed_dir=None,
        session: Optional[requests.Session] = None,
    ):
        self.enabled = _env_flag("X_FEED_ENABLED")
        self.bearer_token = os.getenv("X_BEARER_TOKEN", "").strip()
        self.handles = (
            list(handles)
            if handles is not None
            else _parse_handles(os.getenv("X_TRACK_HANDLES", ""))
        )
        self.feed_dir = Path(feed_dir) if feed_dir else DEFAULT_FEED_DIR
        self.session = session or requests.Session()
        self.connected = False
        self._user_ids: Dict[str, str] = {}
        self._since_ids: Dict[str, str] = {}
        self._last_poll: Dict[str, float] = {}
        self._latest_post: Dict[str, dict] = {}

    # -- DataProvider ----------------------------------------------------

    def connect(self) -> bool:
        """Resolve handle -> user id. Returns ``False`` unless fully enabled.

        Every failure mode is a clean ``False`` (logged), never an exception:
        this provider must be safe to construct and probe anywhere without a
        token in the environment.
        """
        if not self.enabled:
            logger.info("[XProvider] X_FEED_ENABLED is off; feed disabled")
            return False
        if not self.bearer_token:
            logger.warning("[XProvider] X_BEARER_TOKEN not set; feed disabled")
            return False
        if not self.handles:
            logger.warning("[XProvider] X_TRACK_HANDLES empty; feed disabled")
            return False

        try:
            resp = self.session.get(
                f"{self.API_BASE}/users/by",
                headers=self._headers(),
                params={"usernames": ",".join(self.handles)},
                timeout=self.TIMEOUT_S,
            )
            resp.raise_for_status()
            users = resp.json().get("data") or []
        except Exception as exc:  # noqa: BLE001 - a dead feed is a log line
            logger.warning("[XProvider] user resolution failed: %s", exc)
            return False

        self._user_ids = {
            u["username"].lower(): str(u["id"])
            for u in users
            if u.get("username") and u.get("id")
        }
        missing = [h for h in self.handles if h.lower() not in self._user_ids]
        if missing:
            logger.warning("[XProvider] unresolved handles: %s", ",".join(missing))
        self.connected = bool(self._user_ids)
        logger.info(
            "[XProvider] connected: %d/%d handles resolved",
            len(self._user_ids),
            len(self.handles),
        )
        return self.connected

    def fetch_latest(self, symbol: str = None) -> Optional[MarketData]:
        """Newest post for a handle (``symbol``), polling if the interval allows.

        Returns ``None`` while disabled/unconnected, for an untracked handle,
        or before any post has been seen. Price fields are zero — this is a
        text feed, not a quote feed; the post rides in ``extra``.
        """
        if not (self.enabled and self.connected):
            return None
        handle = (symbol or (self.handles[0] if self.handles else "")).lstrip("@")
        if not handle or handle.lower() not in self._user_ids:
            return None

        new_posts = self.poll_handle(handle)
        latest = self._latest_post.get(handle.lower())
        if latest is None:
            return None
        return MarketData(
            symbol=handle,
            timestamp=datetime.now(timezone.utc),
            price=0.0,
            volume=0,
            bid=0,
            ask=0,
            extra={
                "source": "x_api_v2",
                "post_id": latest.get("id"),
                "post_text": latest.get("text"),
                "post_created_at": latest.get("created_at"),
                "new_posts_this_poll": len(new_posts),
                "since_id": self._since_ids.get(handle.lower()),
            },
        )

    # -- polling ---------------------------------------------------------

    def poll_handle(self, handle: str) -> List[dict]:
        """Poll one handle's timeline; append new posts to the tape.

        Respects the per-handle >=60s floor: a call inside the window makes NO
        network request and returns ``[]``. Any API failure is logged and
        returns ``[]`` — the tape degrades to gaps, never to a crash.
        """
        key = handle.lower()
        user_id = self._user_ids.get(key)
        if not user_id:
            return []

        now = time.monotonic()
        last = self._last_poll.get(key)
        if last is not None and (now - last) < self.MIN_POLL_INTERVAL_S:
            return []
        self._last_poll[key] = now

        params = {
            "max_results": self.MAX_RESULTS_PER_POLL,
            "tweet.fields": "created_at",
        }
        since_id = self._since_ids.get(key)
        if since_id:
            params["since_id"] = since_id

        try:
            resp = self.session.get(
                f"{self.API_BASE}/users/{user_id}/tweets",
                headers=self._headers(),
                params=params,
                timeout=self.TIMEOUT_S,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[XProvider] poll failed for @%s: %s", handle, exc)
            return []

        posts = body.get("data") or []
        newest_id = (body.get("meta") or {}).get("newest_id")
        if newest_id is None and posts:
            newest_id = posts[0].get("id")
        if newest_id:
            # since_id is the whole cost model: the next poll reads only posts
            # newer than this, so every post is billed at most once.
            self._since_ids[key] = str(newest_id)

        if posts:
            # Timeline is newest-first; posts[0] is the latest.
            self._latest_post[key] = posts[0]
            self._append_to_tape(handle, posts)
        return posts

    # -- tape ------------------------------------------------------------

    def _append_to_tape(self, handle: str, posts: List[dict]) -> None:
        """Append raw posts to ``x_posts_<UTCdate>.jsonl`` (one line each)."""
        fetched_at = datetime.now(timezone.utc).isoformat()
        path = self.feed_dir / f"x_posts_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        try:
            self.feed_dir.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                for post in posts:
                    f.write(
                        json.dumps(
                            {
                                "handle": handle,
                                "fetched_at": fetched_at,
                                "post": post,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        except OSError as exc:
            logger.warning("[XProvider] tape append failed (%s): %s", path, exc)

    # -- helpers ---------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.bearer_token}"}


__all__ = ["DEFAULT_FEED_DIR", "XProvider"]
