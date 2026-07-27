"""Purge stale simulation state before deploy (Phase 0, FR-0.6).

Two maintenance actions, both idempotent and safe on already-clean state:

1. ``exchange_state.json`` — removes any "open" position whose quantity is
   <= 0. These are shells left by the pre-fix final-partial-close ordering
   bug (state was persisted before the emptied position was removed from
   the open list), e.g. the stuck id-1582 KXBTC15M shell from 2026-07-06.
   All PnL for such shells was already booked at their partial closes, so
   removal is pure hygiene: no journal row, no PnL change. The file is
   rewritten atomically with every other field preserved.

2. ``strategy_win_rates.json`` — archives a legacy-format file (cumulative
   ``[wins, total]`` counters, including the poisoned "ML BTC 15m":
   [30, 1048]) to ``strategy_win_rates.legacy.json``, then writes the new
   FR-0.6 windowed format. Entries already in the new
   ``{"window": [...], "updated": ...}`` shape are kept; legacy entries are
   deliberately dropped — the pivot resets win-rate history so Kelly sizing
   restarts from the neutral 0.5 prior. A missing file is initialised to
   the new empty format (``{}``).

Usage:
    python scripts/purge_stale_state.py
    python scripts/purge_stale_state.py --state-file data/exchange_state.json \
        --win-rates data/strategy_win_rates.json
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

DEFAULT_STATE_FILE = os.path.join("data", "exchange_state.json")
DEFAULT_WIN_RATES = os.path.join("data", "strategy_win_rates.json")


def _atomic_write_json(path: str, payload) -> None:
    """Write JSON atomically (temp file + os.replace) in the target dir."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _is_new_format_entry(value) -> bool:
    """True if a win-rate entry is already in the FR-0.6 windowed format."""
    return isinstance(value, dict) and isinstance(value.get("window"), list)


def purge_exchange_state(path: str) -> bool:
    """Remove qty<=0 'open' positions from an exchange state file.

    Returns True on success (including nothing-to-do), False on error.
    """
    print(f"[exchange-state] {path}")
    if not os.path.exists(path):
        print("[exchange-state]   file not found — nothing to purge")
        return True

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"[exchange-state]   ERROR: could not parse file ({exc}); left untouched")
        return False

    positions = data.get("positions", [])
    if not isinstance(positions, list):
        print("[exchange-state]   ERROR: 'positions' is not a list; left untouched")
        return False

    kept, removed = [], []
    for pos in positions:
        qty = pos.get("quantity", 0) if isinstance(pos, dict) else 0
        if isinstance(qty, (int, float)) and qty <= 0:
            removed.append(pos)
        else:
            kept.append(pos)

    if not removed:
        print(
            f"[exchange-state]   already clean: {len(kept)} open position(s), "
            "none with qty<=0"
        )
        return True

    data["positions"] = kept
    try:
        _atomic_write_json(path, data)
    except Exception as exc:
        print(f"[exchange-state]   ERROR: rewrite failed ({exc})")
        return False

    for pos in removed:
        print(
            "[exchange-state]   REMOVED shell: id={} symbol={} qty={} "
            "original_qty={} opened={} strategy={}".format(
                pos.get("id"),
                pos.get("symbol"),
                pos.get("quantity"),
                pos.get("original_quantity"),
                pos.get("open_time"),
                pos.get("strategy_name"),
            )
        )
    print(
        f"[exchange-state]   purged {len(removed)} qty<=0 shell(s); "
        f"{len(kept)} open position(s) kept; closed_trades untouched "
        f"({len(data.get('closed_trades', []))} rows)"
    )
    return True


def reset_win_rates(path: str) -> bool:
    """Archive legacy-format win rates and write the FR-0.6 windowed format.

    Returns True on success (including nothing-to-do), False on error.
    """
    print(f"[win-rates] {path}")
    if not os.path.exists(path):
        _atomic_write_json(path, {})
        print("[win-rates]   file not found — initialised new empty format {}")
        return True

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print(f"[win-rates]   ERROR: could not parse file ({exc}); left untouched")
        return False

    if not isinstance(data, dict):
        print("[win-rates]   ERROR: file is not a JSON object; left untouched")
        return False

    kept = {k: v for k, v in data.items() if _is_new_format_entry(v)}
    legacy_keys = [k for k in data if k not in kept]

    if not legacy_keys:
        print(
            f"[win-rates]   already clean: {len(kept)} new-format entr"
            f"{'y' if len(kept) == 1 else 'ies'}, no legacy entries"
        )
        return True

    # Archive the original file before dropping legacy entries.
    base, ext = os.path.splitext(path)
    archive = f"{base}.legacy{ext or '.json'}"
    if os.path.exists(archive):
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        archive = f"{base}.legacy-{ts}{ext or '.json'}"
    try:
        shutil.copy2(path, archive)
    except Exception as exc:
        print(f"[win-rates]   ERROR: archive to {archive} failed ({exc}); aborting")
        return False
    print(f"[win-rates]   archived legacy file -> {archive}")

    try:
        _atomic_write_json(path, kept)
    except Exception as exc:
        print(f"[win-rates]   ERROR: rewrite failed ({exc})")
        return False

    for key in legacy_keys:
        print(f"[win-rates]   DROPPED legacy entry: {key!r} = {data[key]!r}")
    print(
        f"[win-rates]   wrote new format: {len(kept)} entr"
        f"{'y' if len(kept) == 1 else 'ies'} kept, "
        f"{len(legacy_keys)} legacy entr"
        f"{'y' if len(legacy_keys) == 1 else 'ies'} archived+dropped "
        "(pivot reset: Kelly restarts from the neutral 0.5 prior)"
    )
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge stale exchange/win-rate state (Phase 0, FR-0.6)."
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help=f"Path to exchange_state.json (default: {DEFAULT_STATE_FILE})",
    )
    parser.add_argument(
        "--win-rates",
        default=DEFAULT_WIN_RATES,
        help=f"Path to strategy_win_rates.json (default: {DEFAULT_WIN_RATES})",
    )
    args = parser.parse_args(argv)

    print("=== purge_stale_state (Phase 0 / FR-0.6) ===")
    ok_state = purge_exchange_state(args.state_file)
    ok_rates = reset_win_rates(args.win_rates)
    print("=== done ({}) ===".format("OK" if ok_state and ok_rates else "ERRORS"))
    return 0 if (ok_state and ok_rates) else 1


if __name__ == "__main__":
    sys.exit(main())
