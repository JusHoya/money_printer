"""Discover Kalshi 15-minute crypto market coverage.

Queries live Kalshi API for every series that has active or initialized
15-minute contracts. Targets patterns like KXBTC15M, KXETH15M, KXSOL15M, etc.
Writes findings to stdout + data/crypto_15m_universe.json.
"""

import os
import sys
import json
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.data.kalshi_provider import KalshiProvider  # noqa: E402


def discover():
    print("--- Kalshi 15-minute Crypto Universe Discovery ---")

    k_id = os.getenv("KALSHI_KEY_ID")
    k_key = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    k_url = "https://api.elections.kalshi.com/trade-api/v2"

    provider = KalshiProvider(k_id, k_key, k_url, read_only=True)
    if not provider.connect():
        print("Failed to connect to Kalshi.")
        return 1

    # Discovery — enumerate active markets via the authenticated
    # search_markets endpoint (plain session.get hits anonymous rate limits).
    print("\n[1/2] Enumerating active markets (authenticated)...")

    series_seen = {}  # series_ticker -> {"count": N, "sample_ticker": "...", "event_ticker": ...}
    cursor = None
    pages = 0
    total = 0
    for _ in range(200):
        params = {"limit": 200, "status": "active"}
        if cursor:
            params["cursor"] = cursor
        try:
            time.sleep(0.2)  # Gentle on rate limit
            result = provider.search_markets(**params)
            if isinstance(result, tuple):
                markets, cursor = result
            else:
                markets, cursor = result, None
            if not markets:
                break
            for m in markets:
                st = m.get("series_ticker") or "UNKNOWN"
                if st not in series_seen:
                    series_seen[st] = {
                        "count": 0,
                        "sample_ticker": m.get("ticker"),
                        "sample_event": m.get("event_ticker"),
                        "sample_title": m.get("title"),
                    }
                series_seen[st]["count"] += 1
            pages += 1
            total += len(markets)
            if not cursor:
                break
        except Exception as e:
            print(f"  Exception: {e}")
            break

    print(
        f"  Scanned {total} markets across {pages} pages, found {len(series_seen)} series"
    )

    # Filter: 15-minute crypto-looking series.
    # Known patterns: KXBTC15M, KXETH15M, KXSOL15M, KXDOGE15M, KXXRP15M...
    # Also catch generic KX*<SYMBOL>15M and anything with 15M in the event.
    crypto_symbols = (
        "BTC",
        "ETH",
        "SOL",
        "DOGE",
        "XRP",
        "ADA",
        "AVAX",
        "BNB",
        "LINK",
        "MATIC",
        "LTC",
        "DOT",
        "ATOM",
        "TRX",
        "BCH",
    )
    candidates = {}
    for st, info in series_seen.items():
        event_t = (info.get("sample_event") or "").upper()
        is_15m = "15M" in st.upper() or "15M" in event_t
        matched_symbol = None
        for sym in crypto_symbols:
            if sym in st.upper() or sym in event_t:
                matched_symbol = sym
                break
        if is_15m and matched_symbol:
            candidates[st] = {
                "asset": matched_symbol,
                "active_markets": info["count"],
                "sample_ticker": info["sample_ticker"],
                "sample_event": info["sample_event"],
                "sample_title": info.get("sample_title"),
            }

    print(f"\n[2/2] Filtered 15-minute crypto candidates: {len(candidates)}")
    print()
    for st in sorted(candidates):
        c = candidates[st]
        print(
            f"  {st:20s} asset={c['asset']:6s} markets={c['active_markets']:4d} "
            f"sample={c['sample_ticker']}"
        )

    # Also write the full series list for reference
    out_path = os.path.join("data", "crypto_15m_universe.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "scanned_markets": total,
                "all_series_count": len(series_seen),
                "crypto_15m_candidates": candidates,
                "all_series_names": sorted(series_seen.keys()),
            },
            f,
            indent=2,
            default=list,
        )
    print(f"\nWrote {out_path}")

    # Also print the broader active-series list, grouped by heuristic,
    # so the user can spot anything that looked crypto-like but didn't match.
    print("\n--- All series names (alphabetical) ---")
    near_crypto = []
    for st in sorted(series_seen.keys()):
        if any(sym in st.upper() for sym in crypto_symbols):
            near_crypto.append(st)
    for st in near_crypto:
        print(f"  {st}")

    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ImportError:
        pass
    sys.exit(discover())
