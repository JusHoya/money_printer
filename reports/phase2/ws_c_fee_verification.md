# Workstream C — Live Kalshi fee-schedule verification for `KXHIGH*`

**Date:** 2026-07-26 (fetches timestamped UTC, i.e. 2026-07-27 early UTC)
**Scope:** PRD FR-2.4 / Phase 2 exit criterion 5. The go/no-go verdict is
highly sensitive to fees, so the fee model workstream E uses must be verified
against the live exchange, not inherited from a 3-day-old dump.
**Author:** Workstream C. `src/core/fee_calculator.py` is orchestrator-reserved
and was **not** edited; proposed diffs are at the end of this document.

---

## 1. Verdict

| | `KXHIGHNY` / `KXHIGHCHI` / `KXHIGHLAX` / `KXHIGHMIA` |
|---|---|
| **Maker fee** | **$0.00** — weather is not on the maker-fee list, and the published maker multiplier defaults to **M = 0** |
| **Taker fee** | `ceil_to_cent(0.07 × C × P × (1 − P))` — the standard formula at M = 1 |
| **Settlement fee** | **$0.00** — "There is no settlement fee" |

Formula workstream E can call:

```python
import math

def kxhigh_maker_fee(price: float, contracts: int) -> float:
    """Kalshi maker fee for the KXHIGH* weather series. Always $0.00.

    Verified 2026-07-27T03:22Z: /series/KXHIGH{NY,CHI,LAX,MIA} all report
    fee_type="quadratic" (not "quadratic_with_maker_fees"), and the published
    maker formula's multiplier defaults to M=0 for any series absent from the
    Non-Standard Fees table -- no KXHIGH* series appears on it.
    """
    return 0.0

def kxhigh_taker_fee(price: float, contracts: int) -> float:
    """fees = round up(1 x 0.07 x C x P x (1-P)), rounded UP to the cent.

    The round(.., 9) removes binary-float error: 0.07*100*0.10*0.90 evaluates
    to 0.6300000000000001, and a bare ceil turns Kalshi's published $0.63 into
    $0.64.
    """
    if price <= 0 or price >= 1.0 or contracts <= 0:
        return 0.0
    raw = 0.07 * contracts * price * (1.0 - price)
    return math.ceil(round(raw * 100, 9)) / 100.0
```

The fee is charged **once per executed trade on the whole order**, not per
contract, and the rounding applies to the order total.

---

## 2. Evidence A — live series metadata (first-party, current)

`fee_type` is the field the 2026-07-24 review used to build
`review_2026_07_24/landscape_out/maker_fee_series.json`
(`review_2026_07_24/probe_kalshi_trades.py:31` filters
`fee_type == "quadratic_with_maker_fees"`). Re-probing that exact field today
re-verifies the same claim against the live exchange rather than the dump.

```
2026-07-27T03:22:51Z  GET https://api.elections.kalshi.com/trade-api/v2/series/KXHIGHNY
                      -> HTTP 200  fee_type="quadratic"  fee_multiplier=1  category="Climate and Weather"
2026-07-27T03:22:52Z  GET .../series/KXHIGHCHI  -> HTTP 200  fee_type="quadratic"  fee_multiplier=1
2026-07-27T03:22:52Z  GET .../series/KXHIGHLAX  -> HTTP 200  fee_type="quadratic"  fee_multiplier=1
2026-07-27T03:22:53Z  GET .../series/KXHIGHMIA  -> HTTP 200  fee_type="quadratic"  fee_multiplier=1
2026-07-27T03:22:53Z  GET .../series/KXAAAGASM -> HTTP 200  fee_type="quadratic_with_maker_fees"  fee_multiplier=1
```

Full response body for `KXHIGHNY` (fetched 2026-07-27T03:19Z):

```json
{"series": {"category": "Climate and Weather",
            "fee_multiplier": 1,
            "fee_type": "quadratic",
            "frequency": "daily",
            "last_updated_ts": "2026-03-16T15:04:55.113254Z",
            "settlement_sources": [{"name": "NWS Climatological Report",
              "url": "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC"}],
            "ticker": "KXHIGHNY",
            "title": "Highest temperature in NYC"}}
```

`KXAAAGASM` (the Phase 4 gas series) is the positive control: it *is*
`quadratic_with_maker_fees`, confirming the field discriminates rather than
being constant. Both bodies are committed verbatim at
`tests/fixtures/ladders/series_kxhighny.json` and `series_kxaaagasm.json` and
pinned by `tests/test_kalshi_history.py::test_weather_series_is_not_on_the_maker_fee_list`.

The market-level endpoint carries **no** fee fields — checked
`GET /markets/KXHIGHNY-26JUL17-B85.5`, whose only related keys are
`notional_value_dollars: "1.0000"` and `price_level_structure: "linear_cent"`.
`GET /exchange/schedule` and `/exchange/status` contain no fee data either.
So the series-level `fee_type` is the only fee metadata the API exposes.

## 3. Evidence B — the published fee schedule

- **URL:** <https://kalshi.com/docs/kalshi-fee-schedule.pdf>
- **Fetched:** 2026-07-27T03:19:01Z, HTTP 200, 382,507 bytes
- **SHA-256:** `815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c`
- **Document footer on every page:** "Last updated and effective: July 7, 2026"
  (i.e. the schedule in force is **newer** than the 2026-07-24 dump, so
  re-verification was warranted)

Page 2, verbatim (whitespace normalised):

> Trading fees are only charged for orders that are immediately matched with
> orders sitting on the orderbook. Trading fees are **not** charged for orders
> placed that are not immediately matched and are instead left as resting
> orders on the orderbook **unless they are included in our "Maker Fees"
> section.**
>
> `fees = round up(M x 0.07 x C x P x (1-P))`
> P = the price of a contract in dollars (50 cents is 0.5)
> C = the number of contracts being traded
> M = the multiplier for each contract (**default is 1** unless otherwise indicated)
>
> **Maker Fees** `fees = round up(M x 0.0175 x C x P x (1-P))`
> P = the price of a contract in dollars (50 cents is 0.5)
> C = the number of contracts being traded
> M = the multiplier for each contract (**default is 0** unless otherwise indicated)

Page 3, verbatim: "**Settlement Fees** — There is no settlement fee."

Pages 6–11 are the **Non-Standard Fees** table: every series with a
non-default multiplier, each row `Series | Maker Multiplier | Taker Multiplier`.
86 rows parse out of pages 6–11, including `KXAAAGASM US gas price 1 1`,
`KXNFLGAME 1 1`, `KXFED 1 1`, `KXBTCY 0 0`. **No `KXHIGH*` series and no
weather/temperature product appears anywhere in that table.** Measured over the
full extracted text of all 12 pages: `KXHIGH` 0 occurrences, `temperature` 0,
`Temperature` 0, `weather` 0, `Weather` 0, `Climate` 0.

Therefore weather takes the defaults: **taker M = 1 → 7% quadratic; maker
M = 0 → $0.00.** Two independent sources (live API `fee_type`, published
schedule) agree.

## 4. Evidence C — the published fee table validates the rounding rule

The schedule's prose says the round-up is "to a centicent", but its own
**General Trading Fees Table** (pages 4–5, 21 price rows × 2 size columns) is
unambiguously **cents**. Every published value is reproduced exactly by
`ceil_to_cent(0.07 · C · P · (1−P))`:

| P | published, C=1 | published, C=100 | raw 0.07·C·P·(1−P), C=100 |
|---|---|---|---|
| 0.01 | $0.01 | $0.07 | 0.0693 |
| 0.05 | $0.01 | $0.34 | 0.3325 |
| 0.10 | $0.01 | $0.63 | 0.6300 |
| 0.25 | $0.02 | $1.32 | 1.3125 |
| 0.50 | $0.02 | $1.75 | 1.7500 |
| 0.90 | $0.01 | $0.63 | 0.6300 |
| 0.99 | $0.01 | $0.07 | 0.0693 |

All 21 rows are committed at
`tests/fixtures/ladders/kalshi_fee_table_2026_07_07.json` and asserted by
`tests/test_kalshi_history.py::test_reference_taker_formula_reproduces_every_published_row`
(21/21 exact, both size columns).

---

## 5. Findings against `src/core/fee_calculator.py` (NOT edited — proposed only)

### Finding F1 — floating-point ceil overstates the taker fee (7 of 21 published rows)

`taker_fee()` computes `math.ceil(raw * 100) / 100`. When the exact fee lands
on a whole cent, binary-float error pushes it one ULP above and the `ceil`
adds a full cent:

```
>>> 0.07 * 100 * 0.10 * 0.90
0.6300000000000002
>>> taker_fee(0.10, 100)
0.64          # Kalshi publishes $0.63
```

Measured now: `taker_fee` matches **14 of 21** published rows. All 7 misses
are on the 100-contract column and all overstate by exactly $0.01
(P = 0.10, 0.20, 0.40, 0.50, 0.60, 0.70, 0.80). Reproduce:

```powershell
$env:PYTHONPATH = "."
python -c "from src.core.fee_calculator import taker_fee; print(taker_fee(0.10,100))"   # 0.64, published 0.63
```

Direction of bias: fees are overstated, so the go/no-go is made *too* pessimistic —
but wrongly, and a fee model that disagrees with the exchange's own published
table cannot back an EV verdict.

**Proposed diff:**

```diff
 def maker_fee(price: float, contracts: int = 1) -> float:
     """Compute maker fee in dollars (rounded up to nearest cent)."""
     if price <= 0 or price >= 1.0 or contracts <= 0:
         return 0.0
     raw = MAKER_RATE * contracts * price * (1.0 - price)
-    return math.ceil(raw * 100) / 100.0
+    # round(.., 9) first: when the exact fee lands on a whole cent, binary
+    # float error puts it one ULP above and a bare ceil adds a full cent.
+    return math.ceil(round(raw * 100, 9)) / 100.0


 def taker_fee(price: float, contracts: int = 1) -> float:
     """Compute taker fee in dollars (rounded up to nearest cent)."""
     if price <= 0 or price >= 1.0 or contracts <= 0:
         return 0.0
     raw = TAKER_RATE * contracts * price * (1.0 - price)
-    return math.ceil(raw * 100) / 100.0
+    return math.ceil(round(raw * 100, 9)) / 100.0
```

Verified: with this change the reference implementation reproduces **21/21**
published rows on both size columns.

### Finding F2 — a maker fee is charged where the exchange charges none

`maker_fee()` unconditionally applies `MAKER_RATE = 0.0175`. For `KXHIGH*` the
true maker fee is **$0.00**. The overcharge is worst exactly where FR-3.1(a)
wants to trade: at P = 0.10, `maker_fee(0.10, 1)` returns **$0.01** — a full
cent, 10% of the premium, on a contract that costs nothing to rest.

This matters enormously for the go/no-go. PRD FR-3.3 makes the flagship
**maker-first**; if the EV report charges 1.75% on the maker path, it is
scoring a fee the exchange does not levy and can HALT a strategy that is
actually +EV.

**Proposed change** (series-aware, fail-loud rather than defaulting):

```python
# Series whose maker orders are billed. Source: the "Non-Standard Fees" table
# of the Kalshi fee schedule (effective 2026-07-07) and the live API's
# series.fee_type == "quadratic_with_maker_fees". The published maker
# multiplier defaults to M=0, so a series absent from that table pays $0.
MAKER_FEE_SERIES_FEE_TYPE = "quadratic_with_maker_fees"

def maker_fee(price, contracts=1, series_fee_type="quadratic"):
    if series_fee_type != MAKER_FEE_SERIES_FEE_TYPE:
        return 0.0            # M = 0 -> no maker fee (all KXHIGH* weather)
    ...
```

Interim, non-invasive option if a signature change is too disruptive
mid-sprint: workstream E calls `kxhigh_maker_fee` from §1 directly and does
not route the weather maker path through `fee_calculator.maker_fee`. That is
what this workstream recommends for the Phase 2 report, with the module fix
landing before Phase 3 execution.

### Finding F3 — `ev_after_fees` double-charges a hold-to-settlement weather position

```python
# src/core/fee_calculator.py:92
# Binary contracts pay fees on both entry and exit (round-trip), so
# fee_per_contract is doubled
return probability - price - 2 * fee_per
```

That is right for a position you close by trading out of it. It is wrong for
weather: PRD FR-1.5 holds weather positions **to settlement**, and the fee
schedule states there is **no settlement fee**. A held-to-settlement contract
pays the **entry fee only**.

Combined with F2, the current model charges a maker-first weather entry
`2 × $0.01 = $0.02` per contract where the exchange charges `$0.00`. At the
far-bracket prices FR-3.1(a) targets (roughly 3–15¢) that phantom 2¢ is on the
order of the entire modelled edge.

**Proposed change:** give `ev_after_fees` an explicit exit model rather than a
hard-coded round trip:

```diff
-def ev_after_fees(probability, price, contracts=1, is_maker=True):
+def ev_after_fees(probability, price, contracts=1, is_maker=True,
+                  exit_mode="trade_out"):
+    """exit_mode="settlement" charges the entry fee only.
+
+    Kalshi levies no settlement fee, so a position held to expiry (PRD FR-1.5,
+    all weather) pays once. "trade_out" keeps the round-trip behaviour for any
+    strategy that closes by trading.
+    """
     fee_per = compute_fee(price, 1, is_maker).per_contract
-    return probability - price - 2 * fee_per
+    legs = 1 if exit_mode == "settlement" else 2
+    return probability - price - legs * fee_per
```

---

## 6. Net effect on the Phase 2 EV report

Per contract, entering at price `P` and holding to settlement:

| path | fee charged today by `fee_calculator` | fee actually charged by Kalshi |
|---|---|---|
| maker entry, P = 0.05 | 2 × $0.01 = **$0.02** | **$0.00** |
| maker entry, P = 0.10 | 2 × $0.01 = **$0.02** | **$0.00** |
| maker entry, P = 0.50 | 2 × $0.01 = **$0.02** | **$0.00** |
| taker entry, P = 0.05 | 2 × $0.01 = **$0.02** | **$0.01** |
| taker entry, P = 0.10 | 2 × $0.01 = **$0.02** | **$0.01** |
| taker entry, P = 0.50 | 2 × $0.02 = **$0.04** | **$0.02** |

(1-contract rounding is punishing on the taker side: the $0.01 minimum is
20 % of a 5¢ contract. Workstream E should compute EV at a realistic order
size — the fee is rounded on the **order total**, so at C = 20, P = 0.05 the
taker fee is `ceil(0.07·20·0.05·0.95 → 0.0665)` = $0.07, i.e. 0.35¢/contract,
not 1¢/contract. Modelling fees per-contract-at-C=1 overstates the taker cost
by ~3× at far-bracket prices.)

Bottom line for FR-2.4: **the maker path on weather is fee-free**, and the
1¢ adverse-fill allowance EC-5 requires is therefore the dominant modelled
cost on that path, not fees.

---

## 7. Reproduction

```powershell
$env:PYTHONPATH = "."
$env:OMP_NUM_THREADS = "2"; $env:OPENBLAS_NUM_THREADS = "2"
$env:MKL_NUM_THREADS = "2"; $env:NUMEXPR_NUM_THREADS = "2"

# Live series fee metadata (anonymous, HTTP 200)
python -c @'
import requests
B="https://api.elections.kalshi.com/trade-api/v2"
for t in ("KXHIGHNY","KXHIGHCHI","KXHIGHLAX","KXHIGHMIA","KXAAAGASM"):
    d=requests.get(f"{B}/series/{t}",timeout=20).json()["series"]
    print(t, d["fee_type"], d["fee_multiplier"])
'@

# Published schedule (pin the hash before trusting the text)
python -c @'
import requests,hashlib
r=requests.get("https://kalshi.com/docs/kalshi-fee-schedule.pdf",
               headers={"User-Agent":"Mozilla/5.0"},timeout=40)
print(r.status_code,len(r.content),hashlib.sha256(r.content).hexdigest())
'@

# Fee-table pinning tests (offline)
python -m pytest tests/test_kalshi_history.py -q -k "fee or maker"
```
