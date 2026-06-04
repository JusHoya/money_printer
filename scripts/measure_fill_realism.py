"""Quantify the penny-floor fill-realism haircut on the ML BTC 15m edge.

Background (2026-06-03 review)
------------------------------
The live paper-trading exchange fills every requested order at the requested
price. A large share of the ML BTC 15m profit comes from buying YES contracts
at the penny floor ($0.01-$0.05) that later settle to $1.00 — lottery wins. In
a real book those resting orders would NOT all fill: at the floor the queue is
deep and adverse selection is brutal — the cheap contract that is *destined to
win* is exactly the one a real market maker would never have sold you at $0.01.

This script estimates how much net PnL and profit factor evaporate under
several penny-floor fill-probability assumptions, with adverse selection
applied to winning $0.01-$0.05 entries (those are the fills you would most
likely have *missed*). It does NOT mutate any state — it reads the closed-trade
ledger and recomputes summary stats. Use it to gauge the real-capital gating
risk before deploying capital.

Usage
-----
    PYTHONPATH=. python scripts/measure_fill_realism.py
    PYTHONPATH=. python scripts/measure_fill_realism.py --state path/to/exchange_state.json
    PYTHONPATH=. python scripts/measure_fill_realism.py --strategy "ML BTC 15m"

The ``pnl`` field on each closed trade is already net of its exit fee but NOT
its entry fee, so the true net per trade is ``pnl - entry_fee`` (mirrors
SimulatedExchange.get_cumulative_net_pnl).
"""

import argparse
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_STATE = _REPO_ROOT / "data" / "exchange_state.json"
_FALLBACK_STATE = _REPO_ROOT / "review_2026_06_03" / "exchange_state.json"

# Penny-floor band (matches SimulatedExchange.PENNY_FLOOR_LO/HI)
PENNY_LO = 0.01
PENNY_HI = 0.05

# Fill-probability scenarios to evaluate (probability a penny-floor order fills)
SCENARIOS = [1.0, 0.5, 0.25]


def _load_closed_trades(state_path: Path) -> list:
    with open(state_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("closed_trades", [])


def _net_pnl(trade: dict) -> float:
    """True net PnL of a trade = stored pnl (post exit-fee) minus entry fee."""
    return float(trade.get("pnl", 0.0)) - float(trade.get("entry_fee", 0.0))


def _is_penny_floor_entry(trade: dict) -> bool:
    p = float(trade.get("entry_price", 0.0))
    return PENNY_LO <= p <= PENNY_HI


def _summarize(trades: list) -> dict:
    """Compute net PnL, win count, and profit factor for a trade list."""
    nets = [_net_pnl(t) for t in trades]
    gross_win = sum(n for n in nets if n > 0)
    gross_loss = -sum(n for n in nets if n < 0)
    net = sum(nets)
    wins = sum(1 for n in nets if n > 0)
    losses = sum(1 for n in nets if n < 0)
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else float("inf")
    return {
        "n": len(trades),
        "net": net,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "wins": wins,
        "losses": losses,
        "pf": pf,
    }


def _apply_fill_haircut(trades: list, p_fill: float) -> list:
    """Return trades as they'd land under penny-floor fill probability ``p_fill``.

    Adverse-selection model: only penny-floor ($0.01-$0.05) *winning* entries
    are at risk of not filling (the cheap contract that wins is exactly the one
    a real book would not have sold you). A deterministic fraction ``1 - p_fill``
    of those winners is removed, sized so the result is stable/reproducible
    rather than dependent on an RNG seed. Penny-floor losers and all non-penny
    trades pass through unchanged — modeling the worst case where you keep every
    losing penny lottery ticket but miss a share of the jackpots.
    """
    if p_fill >= 1.0:
        return list(trades)

    out = []
    penny_win_seen = 0
    for t in trades:
        is_penny = _is_penny_floor_entry(t)
        is_win = _net_pnl(t) > 0
        if is_penny and is_win:
            penny_win_seen += 1
            # Keep a deterministic 1-in-N cadence of winners so the surviving
            # fraction ~= p_fill (e.g. p_fill=0.25 keeps roughly every 4th).
            # Keep this winner if it lands on a kept slot.
            kept_so_far = int((penny_win_seen) * p_fill)
            kept_before = int((penny_win_seen - 1) * p_fill)
            if kept_so_far > kept_before:
                out.append(t)
            # else: dropped (order never filled)
        else:
            out.append(t)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=str,
        default=None,
        help="Path to exchange_state.json (defaults to data/, then review_ fallback).",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="ML BTC 15m",
        help='Strategy name filter (default "ML BTC 15m"). Use "ALL" for no filter.',
    )
    args = parser.parse_args()

    if args.state:
        state_path = Path(args.state)
    elif _DEFAULT_STATE.exists():
        state_path = _DEFAULT_STATE
    else:
        state_path = _FALLBACK_STATE

    if not state_path.exists():
        raise SystemExit(f"No exchange state file found at {state_path}")

    closed = _load_closed_trades(state_path)
    if args.strategy.upper() != "ALL":
        closed = [t for t in closed if t.get("strategy_name") == args.strategy]

    if not closed:
        raise SystemExit(
            f"No closed trades for strategy {args.strategy!r} in {state_path}"
        )

    penny = [t for t in closed if _is_penny_floor_entry(t)]
    penny_wins = [t for t in penny if _net_pnl(t) > 0]
    penny_win_pnl = sum(_net_pnl(t) for t in penny_wins)
    baseline = _summarize(closed)

    print("=" * 74)
    print(" PENNY-FLOOR FILL-REALISM HAIRCUT")
    print("=" * 74)
    print(f" state file        : {state_path}")
    print(f" strategy          : {args.strategy}")
    print(f" closed trades     : {len(closed)}")
    print(f" penny-floor entries (${PENNY_LO:.2f}-${PENNY_HI:.2f}): {len(penny)}")
    print(f" penny-floor wins  : {len(penny_wins)}")
    print(
        f" penny-floor win net PnL : ${penny_win_pnl:+.2f}"
        f"  ({(100 * penny_win_pnl / baseline['net']) if baseline['net'] else 0:.1f}% of net)"
    )
    print("-" * 74)
    print(
        f"{'p_fill':>8} {'net PnL':>12} {'haircut':>12} "
        f"{'profit factor':>14} {'wins':>6} {'trades':>7}"
    )
    print("-" * 74)

    base_net = baseline["net"]
    for p in SCENARIOS:
        kept = _apply_fill_haircut(closed, p)
        s = _summarize(kept)
        haircut = s["net"] - base_net
        pf_str = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(
            f"{p:>8.2f} ${s['net']:>10.2f} ${haircut:>+10.2f} "
            f"{pf_str:>14} {s['wins']:>6} {s['n']:>7}"
        )

    print("-" * 74)
    print(
        " Interpretation: at p_fill=0.25 (deep penny-floor queue + adverse\n"
        " selection on winners) the surviving net PnL is the conservative\n"
        " estimate of the real-capital edge. A large drop means the strategy\n"
        " is mostly penny-floor lottery wins and is NOT safe to gate to capital."
    )
    print("=" * 74)


if __name__ == "__main__":
    main()
