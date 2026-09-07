**Astra Seeded — Kalshi bot development PRD**

Version 0.1 · September 6, 2026 · Branch `astra_seeded` · Status: development specification; no live-capital authorization.

**Product objective.** Start with $300, establish positive net trading expectancy using published or established methods, and grow toward $2,000/month of sustainable net trading profit. Individual losing trades are expected. The product succeeds on account-level profit, not win rate. Neither a completion date nor the required final capital is known yet.

This is a fresh strategy track within the existing repository. This PRD governs new `astra_seeded` work. Historical HALTs, source data, sealed roots, and protected runtime files retain their existing protections. Do not inherit the evolutionary factory, forecast confidence as truth, synthetic fill profits, or its cold-start Kelly dependency. Existing settlement reconciliation and validated parsers may be reused through narrow adapters after tests.

**Success definition and capital constraints**

| Measure | Definition / requirement |
|---|---|
| Seed capital | $300 available trading capital; contributions recorded separately from profit |
| Net trading PnL | Realized sales/settlements less cost basis, exchange fees, and allocated data, inference and incremental operating expenses |
| Economic PnL | Net trading PnL plus change in remaining inventory valued at executable liquidation prices; reject stale/unavailable valuations |
| Income milestone | Six consecutive completed calendar months with both net trading PnL and economic PnL at least $2,000/month; no masked losing inventory or deposit-funded profit |
| Additional income | Incentives and cash interest disclosed separately; neither satisfies the trading-profit target |
| Risk | Maximum 10% liquidation-equity drawdown during qualification; capacity and returns measured at the actual deployed capital |
| Withdrawal policy | No automatic withdrawals during seed validation; before income withdrawals, maintain six monthly loan payments ($12,000 at the stated target) outside trading capital |

The six-month qualification is a product criterion, not a guarantee about later returns. At hypothetical net returns of 2%, 5%, or 10% per month, $2,000 average profit requires respectively $100,000, $40,000, or $20,000. At 4¢ net per contract it requires 50,000 contracts/month. These are arithmetic scenarios, not assumed achievable returns. Measure settlement lockup, capital turnover and depth before proposing capital increases. A higher bankroll cannot fix an unprofitable strategy or insufficient opportunities.

**Evidence reviewed and implementation order**

“Established” has three different meanings below. A published profit report, a payoff identity, and an exchange incentive payment are not interchangeable evidence of repeatable net profit.

| Priority / method | Primary evidence available September 6 | Decision |
|---|---|---|
| S1 — Brier-derived forecast allocation | Gu et al. report $200 → $360.67 over 26 Kalshi trading days, 236 filled orders, $26.31 exchange fees, and 50.9% win rate. The experiment used Gemini 3 Pro with search grounding. [Paper, sections 4.3 and D.7](https://arxiv.org/html/2607.06166v1) | First replication candidate. Single short author-reported run; inference costs and repeatability remain unverified. The linked JavaScript trading-history page did not expose an auditable ledger through the research tool. |
| S2 — Complete-outcome basket arbitrage | Established settlement identities; a recent depth-aware empirical study concerns **Polymarket**, not a proven Kalshi opportunity stream. [Gebele et al.](https://arxiv.org/html/2608.00666v1) | Implement a fee/depth-aware detector; no assumed income. No cross-venue transfers or Polymarket conversion assumptions. |
| S3 — Inventory-controlled maker quotes with incentives | Kalshi currently offers a liquidity incentive program, scheduled through January 1, 2027 but changeable or terminable. Rewards depend on relative book contribution. [Program terms](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program) | Implement only after execution simulation and adverse-selection measurement. Program availability establishes a payment mechanism, not positive net market-making PnL. |
| Deferred — New predictive strategies | Legacy weather/gas findings do not establish a deployable edge; generic sports/crypto/mention heuristics have no validated account-level result here | No custom strategy creation, evolutionary search or model training until S1–S3 receive documented verdicts. |

S1's published construction uses the Brier gradient difference, `2*(p-q)`, with repeated adjustment toward a target position. The paper uses a two-hour cadence, a 2–14 day horizon and exclusions including mentions and timing-ambiguous markets. Reproduce the specified method before trying alternative forecasters; incomplete sizing, prompts or execution details must be recorded as replication gaps. Any approximation or new risk cap is a labeled adaptation and cannot inherit the published return claim.

**FR-1 — Explicit strategy contracts**

- **S1 contract:** Consume timestamped probabilities with source evidence and model/prompt identifiers. Implement the mathematical target and actual-inventory delta first. Forecasts inside the bid/ask band generate no new directional target. State explicitly how binary YES/NO targets map to purchased contracts, how previous inventory is reduced, and how scaling/rounding modifies the published construction. Never count a model's estimated EV as validation. Forecaster identity is frozen before prospective results; a cheaper substitute is a separate candidate.
- **S2 contract:** For a verified mutually exclusive, exhaustive set of N outcomes, buying one YES per outcome pays $1; buying one NO per outcome pays $(N−1). Compare those payoffs against all leg costs, depth, quantity-specific fees and adverse execution allowance. Validate complete membership and identical settlement semantics from archived rules. Merely sharing an event ticker is insufficient. Until every leg fills, the position is exposed to execution risk. Batch submission is not assumed atomic. Fully fund gross acquisition cost; credit collateral netting only after its actual behavior is verified. [Kalshi collateral return](https://help.kalshi.com/en/articles/13823816-collateral-return).
- **S3 contract:** Use a fixed, registered spread/inventory rule with post-only orders, inventory skew, order TTLs and cancellation on stale books/news boundaries. No rewards are booked until credited. Estimate rewards as an interval including zero. Record adverse selection and liquidation losses with and without incentives. Freeze a quote rule before evaluation; do not optimize rewards against a hand-picked profitable tape.

S1 and S3 can lose on correct implementations. S2's completed basket payoff does not guarantee profitable execution or continuous opportunity availability.

**FR-2 — Data and provenance**

Use fresh `data/astra_seeded/` roots partitioned into `raw`, `development`, and `sealed`; new code must not discover data through recursive scans of the legacy data tree. Existing `data/ladders_holdout/`, `data/ladders_2026-09/`, and `data/quarantine/` remain inaccessible. Do not copy their labels into metadata or prompts.

Record UTC receipt/exchange timestamps, sequence numbers, raw order-book messages, trades, market lifecycle, rules/version hash, event grouping, resolution/close/expected-settlement times, fee schedules, incentive periods and outages. Kalshi books provide YES and NO **bids**; derive each ask from the opposing bid with matching quantity. Missing levels remain unavailable, not zero-priced. Use decimal prices and quantities. [Order-book specification](https://docs.kalshi.com/api-reference/market/get-market-orderbook).

Save each forecast as an immutable record of question/rules, public-source URLs and timestamps, evidence cutoff, prompt/model version, probability, call cost, and market snapshot. Reject forecasts with late evidence, future timestamps, malformed probabilities, unknown costs, or missing rules. Track actual resolution time independently of trading close. Disputed, canceled and nonbinary settlements follow archived rules; unresolved inventory cannot be labeled a win.

**FR-3 — Fees and executable accounting**

Retrieve series-specific rates and effective dates. General coefficients are 0.07 for takers and 0.0175 for fee-bearing makers; do not assume every maker order is free. The current schedule lists zero maker/taker multipliers for KXBTCY/KXETHY, which does not solve their turnover constraint. [Fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf).

Implement rounding against the applicable account precision, actual fill quantities and the per-order fee accumulator; reconcile trade fees, rounding charges and rebates separately. Preserve historic schedules in replay. Conservative provisional fees may be used for screening, clearly labeled, but are not exchange reconciliation. [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding).

The ledger must track available/reserved cash, orders, individual fills, inventory cost basis, fees, refunds, settlements and external flows. Equity must reconcile independently to exchange records. A market-data update is never proof of our fill. Entering, canceling or emitting a signal is never a realized trade. Settlement labels cannot enter the strategy interface.

**FR-4 — One execution policy in replay and operation**

Build a small independent package under `src/astra_seeded/`; no imports from protected execution engines. The eventual replay and operational adapter must invoke the same allocation/risk/state-transition functions. Inputs are explicit; no hidden global balances or backtest-populated win history.

State transitions: `candidate → reserved → submitted → acknowledged → partially_filled/filled/canceled/rejected`. Recovery from a timeout queries order state using an idempotent client ID before resubmission. Release reservations only when cancellation/rejection is confirmed. Rebalance using actual filled inventory plus outstanding orders. A rejected opportunity may be reconsidered only under the registered retry policy.

Taker simulation consumes contemporaneous depth, includes decision latency, and permits partial fills. Maker simulation requires queue position and subsequent opposing executions; a price touch alone does not fill. Missing depth/sequence gaps halt affected decisions. Multi-leg replay must include partial completion, hedge/unwind costs and cash held during failure. No atomic synthetic basket fills.

**FR-5 — Seed risk policy**

These are proposed defaults for the new implementation, not edits to legacy risk files or evidence that an existing strategy passes:

- Start at $300; no leverage, external borrowing, automatic top-ups or cross-venue capital split.
- Limit unhedged loss per event/related-event group to 2% of equity ($6 initially), total unhedged exposure to 10% ($30), and all open/reserved cash to 50% ($150). Related outcomes aggregate; purchasing more brackets is not diversification.
- Completed verified basket exposure is measured by its worst settlement payoff, but reserve its full acquisition cash and require every intermediate partial-fill state to respect the unhedged limits.
- Halt new exposure at 3% daily loss ($9 initially) or 10% high-water drawdown ($30 initially), including conservative inventory valuation. Stops cannot prevent losses from sudden resolution/gaps; record that residual risk. Missing valuations prohibit new exposure.
- Fixed bounded allocations precede adaptive Kelly. Round quantities down; never force a minimum order above the risk/cash limit. Evaluate proposed positions plus already pending orders atomically within the local account ledger.
- Model forecasts do not authorize exceeding caps. Changing any cap, strategy, fee model or retry policy creates a new registered evaluation version.

**FR-6 — Prospective validation**

Each method gets a registration containing source implementation/version, market universe, account rules, data split, cost model, primary statistic, minimum economically useful effect, sample size/power, maximum sample, alpha allocation and rejection conditions **before** prospective collection. Execution-only analysis can use development data; it cannot license capital.

For a new directional/maker pilot, collect at least 120 calendar days and 60 distinct resolved event groups. Those floors do not imply adequate power. Estimate required sample size from independent development blocks using the planned cash-PnL statistic, test by simulation at the minimum useful effect, and freeze the larger sample before starting. If that sample cannot fit the operating budget/time limit, return NOT_TESTABLE. Do not extend the window after inspecting its PnL. Report drawdown and all zero-trade days.

Compare S1 to no-trade and a fixed-size, same-forecast baseline after costs; S3 to no-trade both excluding and including credited incentives. Bootstrap event-group contributions with time blocks to address shared releases/regimes. Preallocate familywise alpha across the three methods (Bonferroni 0.05/3 is the initial simple policy); no uncorrected winner selection or repeated peeking. A statistically inconclusive strategy is not capital-cleared.

Promotion requires positive lower confidence bound on net economic PnL, positive net trading PnL, modeled capacity sufficient for its proposed allocation, risk limits respected, correct external settlement/fee reconciliation, and execution parity. S2 additionally requires every observed settlement payoff to match its rule certificate and measured leg-risk losses to be included. Unit tests, paper fills, and zero-loss simulations do not prove live profitability.

**Development phases and exit conditions**

| Phase | Concrete deliverable | Exit / stop condition |
|---|---|---|
| P0 — Source replication | Evidence register; source-derived numerical kernels; synthetic fixtures; explicit missing details | Algebra/payoff identities match references; malformed/incomplete inputs refused; no trading-profit claim |
| P1 — Account foundation | Immutable data/forecast records, decimal fee model, cash ledger, risk limits, deterministic execution state machine | Partial-fill, cancellation, restart, cash-reservation and external-reconciliation tests pass; protected diff empty |
| P2 — First method | S1 prospective forecast pipeline and target rebalancing; capped inference costs; fixed-size control | Published-method gaps resolved or adaptation labeled; execution policy registered before data; forecast cost known |
| P3 — Structural detector | S2 live read-only book scanning and payoff certificates | Opportunity report measures feasible quantity, expiry/lockup and leg risk; no candidate is acceptable if costs erase the surplus |
| P4 — Maker trial | S3 incentive discovery, frozen quote rule and conservative queue replay | Maker losses and credited incentives separately measured; thin-book or no-incentive cases cannot be concealed |
| P5 — Paper decision | Frozen prospective scorecards for eligible S1–S3 lanes | PASS / FAIL / NOT_TESTABLE under FR-6; failure does not trigger bespoke strategy invention automatically |
| P6 — Limited live verification | Small explicit capital allocation, read/write adapter, account reconciliation, kill switch | Requires later owner authorization after a concrete reviewable paper result; pass fills/fees/settlements against actual records before scaling |
| P7 — Scale and income | Capacity curve, contribution/withdrawal ledger, cost and risk reporting at each size | Scale only after evidence at prior size; product income criterion met before calling the bot loan-payment ready |

Initial economic kill screen: if 30 days of read-only discovery cannot identify sufficient candidate turnover/depth to plausibly cover the proposed method's measured recurring costs under conservative assumptions, pause its paid forecasting/buildout. This is a feasibility screen, not a statistical profitability verdict. Existing strategies remain off throughout these phases.

**Architecture and operator experience**

One Python service on existing hardware is sufficient initially. Separate pure strategy/risk calculations from collectors, an append-only ledger, replay, and an exchange adapter. Persist a replayable event log and small queryable account database; checkpoint atomically. Build a read-only status command before another dashboard. No GPU training, distributed search, new server purchase or multi-agent orchestration is required.

The operator must be able to see cash, conservative equity, realized/economic PnL, fees, inference spend, incentives, open/pending risk, stale inputs, reconciliation errors, strategy version and current gate in one report. Alert on an actionable state change; do not send routine per-tick narration. External messaging requires its own configured authorization.

**Token, inference and work budget**

- Keep this PRD plus a small status file as the default handoff; read changed files and targeted sources instead of re-reviewing the whole project.
- Run numerical evaluation locally. No LLM calls on order-book updates, inside execution/risk checks or for routine summaries.
- Default external inference spending to zero until explicitly configured. Suggested seed ceiling is $5/month and 20 forecast requests/day, whichever binds first. These are cost controls, not a faithful reproduction of the paper's workload. Hitting the cap skips new forecasts and keeps risk management running.
- Cache forecasts by market/rules/evidence cutoff/model/prompt; never reuse a future observation in replay. Track billed tokens and dollars; do not treat an existing subscription or local hardware as proof of zero marginal operating cost.
- No automated model search. Reproduce one candidate, issue a compact verdict, then advance. Keep targeted tests fast; run broader integration checks at phase boundaries.

**Decisions still requiring evidence**

The first forecaster/API budget, exact source replication parameters, account fee precision/eligibility, sustainable edge, capacity and ultimate capital requirement are unresolved. They are explicit phase dependencies, not reasons to invent performance assumptions. Current sources justify trying established methods; they do not justify promising $2,000/month from this seed.
