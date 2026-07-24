"""Tests for the 2026-06-10 ML-label corruption fix.

FIX 1 (settlement-result labeling) — ``compute_labels_with_settlement`` and
``compute_labels_from_terminal_price`` in ``scripts.train_from_csv``: a
terminal price pinned at ~1.0 is treated as a locked-book artifact and routed
to the Kalshi settlement result instead of being trusted as YES. With no API
available, such contracts are *withheld* (left unlabeled) — never mislabeled.

Phase 0 teardown (2026-07-24): the FIX 2 tests for ``_best_observable_price``
in ``src.bots.btc_15m_bot`` were removed with that deleted bot module. The
``train_from_csv`` labeler remains on disk as an OFFLINE-only training entry
point (PRD FR-0.2), so its label-correctness regression tests stay.

All tests are offline and deterministic (synthetic DataFrames + a fake provider
stub; ``time.sleep`` is monkeypatched to a no-op). The optional end-to-end guard
reproduces the 31.5% -> 0% false-YES result on the production sample and is
skipped when the review data directory is absent.
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts import train_from_csv
from scripts.train_from_csv import (
    compute_labels_from_terminal_price,
    compute_labels_with_settlement,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _contract_df(symbol: str, prices):
    """Build a minimal contract_df for one symbol from a price series."""
    base = pd.Timestamp("2026-06-06T19:00:00")
    rows = [
        {
            "timestamp": base + pd.Timedelta(seconds=30 * i),
            "symbol": symbol,
            "contract_price": float(p),
        }
        for i, p in enumerate(prices)
    ]
    return pd.DataFrame(rows)


class _FakeKalshiProvider:
    """Minimal stand-in for KalshiProvider used by the settlement path."""

    PUBLIC_API_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, result: str, status: str = "finalized"):
        self._result = result
        self._status = status
        self.calls = []

    def _fetch_market_raw(self, symbol, api_url):
        self.calls.append((symbol, api_url))
        return {"status": self._status, "result": self._result}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep the API-loop fast: real ``time.sleep`` would add 0.12s/contract."""
    monkeypatch.setattr(train_from_csv.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """Never read/write the real data/models/settlement_cache.json in tests."""
    cache_path = tmp_path / "settlement_cache.json"
    monkeypatch.setattr(train_from_csv, "_SETTLEMENT_CACHE_PATH", str(cache_path))
    return cache_path


# --------------------------------------------------------------------------- #
# A) (removed) Price-logging helper tests — btc_15m_bot deleted in Phase 0
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# B) Labeler — settlement path (Fix 1)
# --------------------------------------------------------------------------- #

_SYM = "KXBTC15M-26JUN061930-30"


def test_terminal_one_no_longer_labeled_yes():
    """Regression: terminal 1.0 with no API must NOT be labeled YES; withheld."""
    df = _contract_df(_SYM, [0.011, 0.013, 0.009, 0.002, 0.001, 0.001, 1.000])
    labels = compute_labels_with_settlement(df, kalshi_provider=None)
    assert labels.get(_SYM) != 1  # the load-bearing regression assertion
    assert _SYM not in labels  # withheld (unlabeled) when no API resolves it


def test_terminal_one_resolved_to_settlement_no():
    """Locked-book terminal 1.0 routed to settlement -> correct NO from API."""
    df = _contract_df(_SYM, [0.011, 0.002, 0.001, 0.001, 1.000])
    provider = _FakeKalshiProvider(result="no")
    labels = compute_labels_with_settlement(df, kalshi_provider=provider)
    assert labels[_SYM] == 0
    assert provider.calls  # the settlement path was actually exercised


def test_terminal_one_resolved_to_settlement_yes():
    """A genuinely settled-YES contract that locked at 1.0 is recovered as YES.

    The naive "exclude all 1.0 terminals" approach would wrongly drop these;
    the settlement route returns the correct YES.
    """
    df = _contract_df(_SYM, [0.80, 0.90, 0.98, 1.000, 1.000])
    provider = _FakeKalshiProvider(result="yes")
    labels = compute_labels_with_settlement(df, kalshi_provider=provider)
    assert labels[_SYM] == 1


def test_persistent_one_artifact_resolved_by_settlement():
    """Edge case: cleared-book 1.0 persists for the last several samples.

    KXBTC15M-...070530-30 settled NO with final samples 0.001,0.001,0.001,1.0,1.0.
    A penultimate-price heuristic fails here; only settlement is correct.
    """
    sym = "KXBTC15M-26JUN070530-30"
    df = _contract_df(sym, [0.05, 0.01, 0.001, 0.001, 0.001, 1.0, 1.0])
    provider = _FakeKalshiProvider(result="no")
    labels = compute_labels_with_settlement(df, kalshi_provider=provider)
    assert labels[sym] == 0


def test_genuine_no_below_threshold_unaffected():
    """A clearly-NO terminal (<0.15) is labeled NO with no API call."""
    df = _contract_df(_SYM, [0.20, 0.10, 0.05, 0.03])
    provider = _FakeKalshiProvider(result="yes")  # would flip it if called
    labels = compute_labels_with_settlement(df, kalshi_provider=provider)
    assert labels[_SYM] == 0
    assert not provider.calls  # resolved on the fast path, no API needed


def test_genuine_yes_below_lock_unaffected():
    """A genuine YES still trading in [0.85, 0.995) is labeled YES, no API."""
    df = _contract_df(_SYM, [0.70, 0.85, 0.90, 0.92])
    provider = _FakeKalshiProvider(result="no")  # would flip it if called
    labels = compute_labels_with_settlement(df, kalshi_provider=provider)
    assert labels[_SYM] == 1
    assert not provider.calls


def test_fast_path_locked_book():
    """compute_labels_from_terminal_price: terminal 1.0 must NOT be labeled YES."""
    df = _contract_df(_SYM, [0.50, 0.20, 0.01, 1.000])
    labels = compute_labels_from_terminal_price(df)
    assert labels.get(_SYM) != 1
    assert _SYM not in labels  # skipped (dropped) — safe failure mode


def test_fast_path_genuine_yes_below_lock():
    """Fast path still labels a genuine sub-lock YES (terminal in (0.90, 0.995))."""
    df = _contract_df(_SYM, [0.70, 0.88, 0.93])
    labels = compute_labels_from_terminal_price(df)
    assert labels[_SYM] == 1


# --------------------------------------------------------------------------- #
# C) Optional end-to-end guard on the production sample (31.5% -> 0%)
# --------------------------------------------------------------------------- #

_REVIEW = Path(__file__).resolve().parent.parent / "review_2026_06_09"
_VM_CSV = _REVIEW / "vm_csv"
_TRUTH = _REVIEW / "kxbtc15m_truth.json"


@pytest.mark.skipif(
    not (_VM_CSV.exists() and _TRUTH.exists()),
    reason="review_2026_06_09 production sample not present",
)
def test_production_sample_zero_false_yes(monkeypatch):
    """On the real Jun 6-10 VM CSVs, the fixed labeler produces 0 false-YES.

    Truth (Kalshi settlements) is fed via a per-symbol fake provider so that
    locked-book terminals route to ground truth, reproducing the 31.5% -> 0%
    contract-level result without any live API.
    """
    from scripts.train_from_csv import load_session

    truth = json.load(open(_TRUTH, encoding="utf-8"))
    truth_lbl = {t: (1 if v["result"] == "yes" else 0) for t, v in truth.items()}

    frames = []
    for p in sorted(_VM_CSV.glob("data_*.csv")):
        _btc, contract = load_session(str(p))
        if not contract.empty:
            frames.append(contract)
    if not frames:
        pytest.skip("no contract rows loaded from production CSVs")

    contract_df = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["timestamp", "symbol"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    class _TruthProvider:
        PUBLIC_API_URL = "https://api.elections.kalshi.com/trade-api/v2"

        def _fetch_market_raw(self, symbol, api_url):
            if symbol in truth:
                return {"status": "finalized", "result": truth[symbol]["result"]}
            return {"status": "active", "result": ""}

    labels = compute_labels_with_settlement(
        contract_df, kalshi_provider=_TruthProvider()
    )

    # Count false-YES: labeled YES but truth says NO (the corruption signature).
    false_yes = [
        s
        for s, lab in labels.items()
        if lab == 1 and s in truth_lbl and truth_lbl[s] == 0
    ]
    assert false_yes == [], f"{len(false_yes)} false-YES remain: {false_yes[:5]}"
