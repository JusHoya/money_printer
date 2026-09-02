# Markets Expansion — Decision Record, 2026-09-01

Status: **adopted 2026-09-01**. This document records why the sandbox's market
surface expanded, what was verified live before deciding, and the kill criteria
that unwind each piece. It changes nothing about the capital posture: all
execution is `SimulatedExchange`, Kalshi credentials stay read-only, and no
strategy is cleared to trade real capital.

## a. Context

- The project carries **two consecutive HALT verdicts** — weather (Phase 2,
  2026-07-26) and AAA gas (Phase 4, 2026-07-30) — and `PRD.md` has no third
  engine (HANDOFF.md §1). Both HALTs failed the same way: modelled EV
  optimistic against realized, settlement-true outcomes.
- The **2026-08-31 review** re-examined the market landscape rather than the
  strategies: the harvester was watching two proven-dead lanes while the
  exchange had grown whole categories the project had never priced.
- **User direction, 2026-09-01**: expand the harvested market surface and
  start generating a paper PnL history — the dashboard has nothing to show and
  the sim settlement leg has never been exercised (*"No weather position has
  ever been opened"*, HANDOFF.md §2 caveat). Widen what we observe; keep the
  activation bar where HANDOFF.md put it.

## b. What was verified live on 2026-09-01

### Mention markets

- **95 active mention series** discovered via the production V2 API.
- Fees are **quadratic at x1** (standard `0.07·p·(1-p)` formula,
  `fee_multiplier=1`).
- Books are **thin, 1–7c spreads** — small notional moves the price, which
  cuts both ways: no institutional depth, but also room for a small account.
- The viable lane is **base-rate mispricing**: markets priced on narrative
  where the historical base rate of the phrase/mention is knowable from a
  corpus. Viable *for small accounts specifically* because the thin books that
  exclude size are exactly where a $350-gate account can operate.
- **Regulatory risk is live**: Kalshi pulled sports-broadcast mention markets
  on **Aug 19** under a CFTC probe. Insider precedents exist — a teleprompter
  operator settled with the CFTC over mention-market trading, and the Santos
  ban. This category can be delisted wholesale with no notice.

### Sports and crypto — refutations re-checked, not relaxed

The July/August refutations **hold and have strengthened**:

- The `0.07·p·(1-p)` fee formula is unchanged.
- BTC 15-minute: **0.1c spreads against a ~3.5c fee floor** — the arithmetic
  that killed short-horizon crypto is intact.
- Kalshi sports lines now sit **tighter than Pinnacle**; institutional market
  makers have deepened (Cantor, SIG, Jump, Flutter). The retail-edge window
  the July review already scored as closed is more closed.

Two **new screenable shapes** surfaced — screenable, not tradeable:

1. **KXBTCY / KXETHY annual ladders with `fee_multiplier=0`** — a fee holiday,
   **unverified** (the field says 0; no settled round trip confirms it). The
   coarse **$5k strike ladders** pass the coarseness screen that killed the
   gas $0.01 ladder, and the position is a **~4-month lockup** to year-end
   settlement. Harvest-and-watch only.
2. **MLB props at 0.5x fees** (~**1.76c round trip**) — halves the fee floor
   in the one sports niche with it. Moot until the **2027 season**; noted so
   the next reviewer does not rediscover it.

## c. X (Twitter) access for TWEETS-settled markets

- The **official pay-per-use X API** is the only load-bearing path:
  **$0.005/read with 24h dedup**, projecting **~$10–25/mo for 5 handles**,
  latency **<=60s**.
- Kalshi's TWEETS settlement source is itself a **5-minute X API poll**, so an
  official-API poller is **byte-replicable** against the settlement source —
  the same property that makes the IEM CLI pipeline trustworthy for weather.
- **Nitter is dead** (C&D, Aug 2026). Scrapers are **not load-bearing**: a
  settlement-relevant feed cannot depend on an interface X is actively
  killing.
- **Kalshi-side surface, re-verified 2026-09-01 (evening):** the weekly
  `KXELONTWEETS` count ladder this section was written for has listed **no
  event since 2025-04-18** (dormant). `KXPOTUSTWEETS` is now a **monthly
  binary** ("Will @realDonaldTrump tweet in Sep 2026?", YES 0.62/0.69, ~32
  contracts). The other X-settled series (`KXPOPETWEETS`, `KXHUNTERTWEETS`,
  `KXROARINGKITTYTWEETS`, `KXPLATNERTWEET`) are one-offs with no live market.
  The **live post-count ladder is `KXTRUTHSOCIAL`** (weekly, 10 brackets,
  13–17k contracts on the middle brackets, settles Saturday 13:59 UTC from
  Roll Call's Factbase count) — a Truth Social feed, not X. Consequence: the
  X spend has exactly one live consumer today, and tracking only
  @realDonaldTrump (a few X posts a month) makes it cents, not $10–25.
- **Wiring:** the `tweets` bot (`src/bots/tweets_bot.py`, registered
  2026-09-01) harvests the X-settled ladders every tick and, when
  `X_FEED_ENABLED=1`, polls the tracked handles through `XProvider`, writing
  the raw-post tape (`data/x_feed/`) and one `@handle (X)` data-log row per
  poll that returns posts. No strategy behind it; `TWEETS_TRADING_ENABLED`
  is `False`.

## d. Decisions

| # | Decision | Scope |
|---|---|---|
| 1 | **Weather paper trading ON** | Sandbox only, read-only creds, `SimulatedExchange`. Purpose: exercise the untested sim settlement leg and generate PnL history. Explicitly *not* a clearing of the Phase 2 HALT — that still requires the §9.3 pre-registered evidence. |
| 2 | **`mention` + `crypto_annual` harvesters ON, feed-only** | Tape collection only (`MENTION_SERIES` selects the mention series). No signals reach execution. |
| 3 | **Mention strategy scaffold gated `False`** | Stays off pending (i) a base-rate corpus to price against and (ii) encoding the settlement grammar from Kalshi's MENTION.pdf rules — pattern-matching settlement text without the grammar is the inverted-suffix-parser mistake again. |
| 4 | **X provider wired into the feed-only `tweets` bot; poller disabled** | `X_FEED_ENABLED=0` until the user opens an X API account; the Kalshi side (`TWEETS_SERIES`) harvests regardless. No scraper fallback. Only one live X-settled market exists today (§c). |

Operational surface: `deploy/README.md` (envs, healthcheck, autoheal,
redeploy runbook).

## e. Kill criteria

- **CFTC delists mention markets** → archive the mention bot and its tape;
  do not chase replacement phrasings on other series.
- **KXBTCY/KXETHY fee holiday disproven** (a settled round trip shows standard
  fees, or `fee_multiplier` flips to nonzero) → the standard crypto fee
  arithmetic applies and the annual-ladder shape dies with it; the harvester
  reverts to tape-only interest.
- **Any strategy activation** — mention, weather re-clearing, anything on the
  annual ladders — goes through the HANDOFF.md house rules: realized,
  settlement-true outcomes clustered on the correct independent unit,
  reconciled against external ground truth, **no modelled-EV gates**. A
  documented HALT satisfies the exit criterion.
