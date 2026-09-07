"""P0 numerical methods; results are research candidates, never executable orders.

Brier coordinates: https://arxiv.org/html/2607.06166v1
Basket identities: https://arxiv.org/html/2608.00666v1
Quote convention: https://docs.kalshi.com/api-reference/market/get-market-orderbook
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from typing import Sequence

ZERO, ONE = Decimal(0), Decimal(1)


def decimal(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a financial number")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError("Invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("Non-finite decimal")
    return result


def probability(value) -> Decimal:
    result = decimal(value)
    if not ZERO <= result <= ONE:
        raise ValueError("Probability must be in [0, 1]")
    return result


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Timestamp requires an explicit timezone")
    return result


def brier_targets(probabilities, bids, asks, scale_contracts) -> tuple:
    """Return 2 * scale * (p - q), with q clipped to each bid/ask band.

    Signed COORDINATE targets, not YES-equivalent orders. Negative coordinates
    must be mapped through NO contracts by a future exposure-aware adapter.
    No rounding, fee adjustment, risk sizing or forecast-quality claim here.
    """
    p = tuple(probability(x) for x in probabilities)
    bid = tuple(probability(x) for x in bids)
    ask = tuple(probability(x) for x in asks)
    scale = decimal(scale_contracts)
    if len(p) < 2 or len(p) != len(bid) or len(p) != len(ask):
        raise ValueError("At least two aligned outcome coordinates required")
    if abs(sum(p) - ONE) > Decimal("0.000000001") or scale <= 0:
        raise ValueError("Probabilities must sum to one; scale must be positive")
    if any(b > a for b, a in zip(bid, ask)):
        raise ValueError("Crossed quote band")
    reference = tuple(min(a, max(b, x)) for x, b, a in zip(p, bid, ask))
    return tuple(2 * scale * (x - q) for x, q in zip(p, reference))


def rebalance_delta(target, filled, pending) -> tuple:
    """Signed coordinate delta after accounting for acknowledged pending orders."""
    if not len(target) == len(filled) == len(pending):
        raise ValueError("Position vectors must align")
    return tuple(decimal(t) - decimal(f) - decimal(p)
                 for t, f, p in zip(target, filled, pending))


@dataclass(frozen=True)
class Level:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class Book:
    market_id: str
    observed_at: datetime
    yes_bids: tuple
    no_bids: tuple
    taker_multiplier: Decimal

    @classmethod
    def from_dict(cls, row):
        def levels(name):
            out = tuple(Level(probability(p), decimal(q)) for p, q in row[name])
            if any(not ZERO < x.price < ONE or x.quantity <= 0 for x in out):
                raise ValueError("Book levels require positive size and interior prices")
            if len({x.price for x in out}) != len(out):
                raise ValueError("Duplicate book price levels")
            return tuple(sorted(out, key=lambda x: x.price, reverse=True))

        market_id = row["market_id"]
        if not isinstance(market_id, str) or not market_id.strip():
            raise ValueError("Market identifier required")
        yes, no = levels("yes_bids"), levels("no_bids")
        multiplier = decimal(row["taker_multiplier"])
        if multiplier < 0:
            raise ValueError("Fee multiplier cannot be negative")
        if yes and no and yes[0].price + no[0].price > ONE:
            raise ValueError("Crossed binary book")
        return cls(market_id, timestamp(row["observed_at"]), yes, no, multiplier)

    def asks(self, side: str) -> tuple:
        if side not in ("yes", "no"):
            raise ValueError("Side must be yes or no")
        bids = self.no_bids if side == "yes" else self.yes_bids
        return tuple(Level(ONE - x.price, x.quantity) for x in bids)


def acquisition_cost(book: Book, side: str, quantity: int):
    """Conservative per-level all-in cent cost; None means insufficient depth.

    Includes notional alignment at sub-cent prices. This is a SCREENING model,
    not Kalshi's account-specific fee accumulator implementation.
    """
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("Positive whole-contract quantity required")
    remaining, total = Decimal(quantity), ZERO
    for level in book.asks(side):
        fill = min(remaining, level.quantity)
        notional = level.price * fill
        raw_fee = (Decimal("0.07") * book.taker_multiplier * fill
                   * level.price * (ONE - level.price))
        trade_fee = raw_fee.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
        total += (notional + trade_fee).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        remaining -= fill
        if remaining == 0:
            return total
    return None


def scan_baskets(certificate, books: Sequence[Book], *, as_of: datetime,
                 cash_available, max_contracts=10, max_age_seconds=2,
                 max_skew_seconds=1, adverse_per_leg="0.01",
                 min_net_per_contract="0.01") -> list:
    """Screen complete YES/NO baskets against trusted, externally reviewed rules.

    The certificate asserts ordinary binary, mutually exclusive, exhaustive
    outcomes. This function checks consistency; it cannot establish that the
    asserted rules match the exchange. No leg fills or profits are recorded.
    """
    flags = ("mutually_exclusive", "exhaustive", "ordinary_binary", "rules_reviewed")
    if any(certificate.get(k) is not True for k in flags):
        raise ValueError("Reviewed complete ordinary-binary outcome set required")
    if not certificate.get("event_id") or not certificate.get("rules_sha256"):
        raise ValueError("Event and rules provenance required")
    digest = certificate["rules_sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid rules SHA256")
    ids = certificate["market_ids"]
    if (len(ids) < 2 or len(set(ids)) != len(ids)
            or len(books) != len(ids) or {b.market_id for b in books} != set(ids)):
        raise ValueError("Incomplete or duplicate basket membership")
    if as_of.utcoffset() is None:
        raise ValueError("As-of requires timezone")
    if type(max_contracts) is not int or not 1 <= max_contracts <= 1000:
        raise ValueError("Quantity search must be between 1 and 1000")
    age, skew = decimal(max_age_seconds), decimal(max_skew_seconds)
    cash, adverse, minimum = map(decimal, (cash_available, adverse_per_leg, min_net_per_contract))
    if min(age, skew, cash, adverse) < 0 or minimum <= 0:
        raise ValueError("Nonnegative limits and positive minimum edge required")
    if any(b.observed_at.utcoffset() is None or not ZERO <= decimal(
            (as_of - b.observed_at).total_seconds()) <= age for b in books):
        raise ValueError("Stale or future book")
    if decimal((max(b.observed_at for b in books) - min(
            b.observed_at for b in books)).total_seconds()) > skew:
        raise ValueError("Asynchronous basket books")
    results = []
    for side in ("yes", "no"):
        payout_per_set = ONE if side == "yes" else Decimal(len(books) - 1)
        for count in range(1, max_contracts + 1):
            costs = [acquisition_cost(b, side, count) for b in books]
            if any(c is None for c in costs):
                break
            acquisition = sum(costs, ZERO)
            buffer = adverse * len(books) * count
            cost = acquisition + buffer
            net = payout_per_set * count - cost
            if cost <= cash and net >= minimum * count:
                results.append({
                    "event_id": certificate["event_id"], "side": side,
                    "contracts_per_leg": count, "market_ids": list(ids),
                    "acquisition_cost": acquisition, "adverse_allowance": buffer,
                    "cash_required": cost, "conditional_settlement_payout": payout_per_set * count,
                    "conditional_net_surplus": net,
                    "classification": "candidate_only_no_fills_assumed",
                    "fee_model": "conservative_per_level_cent_cost",
                })
    return results
