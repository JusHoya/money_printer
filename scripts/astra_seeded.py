"""Run P0 methods on an explicit offline JSON input. Never connects to an exchange."""
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.astra_seeded.methods import Book, brier_targets, rebalance_delta, scan_baskets, timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON under examples/astra_seeded or data/astra_seeded/development")
    args = parser.parse_args()
    source = args.input.resolve()
    allowed = (ROOT / "examples/astra_seeded", ROOT / "data/astra_seeded/development")
    if not any(source.is_relative_to(p.resolve()) for p in allowed):
        parser.error("Input must be inside an allowed Astra development root")
    try:
        data = json.loads(source.read_text(encoding="utf-8"), parse_float=Decimal)
        forecast = data["brier"]
        targets = brier_targets(forecast["probabilities"], forecast["bids"],
                                forecast["asks"], forecast["scale_contracts"])
        delta = rebalance_delta(targets, forecast["filled"], forecast["pending"])
        candidates = scan_baskets(data["certificate"], [Book.from_dict(b) for b in data["books"]],
                                  as_of=timestamp(data["as_of"]), **data["screen"])
    except (KeyError, ValueError, TypeError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"mode": "offline_research", "input": source.name,
                      "brier_coordinate_targets": targets, "coordinate_delta": delta,
                      "basket_candidates": candidates,
                      "profitability_verified": False}, default=str, indent=2))


if __name__ == "__main__":
    main()
