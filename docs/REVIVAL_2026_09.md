# Revival plan — 2026-09 (PROPOSED, awaiting owner ratification)

Produced 2026-08-31 by a 21-agent adversarially-verified review (4 repo
deep-reads, 6 web research agents, screening, 2 refutation lenses per
candidate, synthesis). This document carries the parts that require the
owner's explicit sign-off. **Nothing here is ratified yet.** Per house rule
"a gate that ranks a loser above a winner is not a gate", the acceptance
thresholds below must be committed BEFORE anyone looks at autumn data —
ratifying them after seeing results would recreate the gate-shopping
failure mode that produced two HALTs.

## 1. The verdict on the goal

$2,000/month on a $350 bankroll is a 571% monthly return. No evidence
surveyed — in the repo or on the web — supports anything within an order of
magnitude of that on Kalshi at this account size. Independent capacity
estimates for every candidate strategy land between ~$80 and ~$350/month at
this bankroll, before proving any edge exists. The honest reframing:

- $350 live capital, IF a strategy passes its gate: target **$100–300/month
  realized**.
- $2k/month becomes a 2027 capital-scaling question, revisited only after
  three consecutive positive live months.

## 2. The strategy decision

Five candidates (from web research + repo assets) were screened; each of the
top five was attacked by two independent adversarial verifiers
(fees/microstructure lens, evidence-quality lens):

| candidate | verdict |
|---|---|
| **Settle the weather question via phase2 §9.3 R1–R5 re-gate** | **SURVIVED both lenses — sole survivor** |
| Two-sided thin-market maker + LIP capture | REFUTED (own tape: negative in all 6 bands) |
| Favorite-longshot bias harvesting | REFUTED (phase2 §6.2: Kalshi weather book is well calibrated; stale fee premise) |
| Sports sharp-line value trading | REFUTED (feed cost > edge at $350; latency-race vs institutional MMs) |
| Underreaction drift, maker-posted | REFUTED (source paper's own executable-price test was negative) |

Salvage item: the one-sided 5F+ buy_no maker residue (+2.29c ±1.11c,
optimistically clustered) gets a free Stage-A tape re-run with strict-cross
fills, date clustering, and live-verified maker fee. Kill if the CI includes
zero.

## 3. Milestones

- **M0 (by 2026-09-15) — infrastructure standing.** Harvester + ladder
  capture + both reconcile timers running 24/7 on home hardware; R1 source
  freeze committed (§4); second copy of the tape archive made; VM-era
  credentials rotated. **Hard kill: not capturing by Sept 15 forfeits the
  autumn season** (Kalshi prunes settled markets after ~60 days; the harvest
  has been down since 2026-08-22 and ladder capture since 2026-07-25).
- **M1 (by ~2026-11-07) — the weather verdict.** Run `scripts/go_no_go.py`
  UNMODIFIED against Sept–Oct ladder dates (strictly outside the original
  2026-05-18..2026-07-25 window). Cost ≈ $0. Both outcomes are wins: PASS
  yields the only rules-compliant edge ever measured; FAIL closes weather
  permanently (HALT #3, house rule 6).
- **M2 (PASS only, Nov–Dec) — capital gate.** Implement the never-built
  FR-5.2 gate (≥50 grouped settled trades, exact binomial p<0.05 vs
  fee-adjusted breakeven, settlement-true PnL>0, realistic fills) and run a
  30-day live-paper sandbox on maia, including the first-ever exercise of
  the sim settlement leg against live data and a top-of-book depth
  measurement design.
- **M3 (gate passed) — $350 live**, target $100–300/month realized.
- **If M1 fails:** harvest-and-hold or retirement. NOT a fourth engine
  evaluated on modelled EV.

## 4. R1 source freeze (REQUIRES RATIFICATION)

Proposed: freeze `gfs_mex` as the sole forecast source for the M1
evaluation, justified only on data predating 2026-09-01 (it was the winning
source on the Phase 2 tape and is the source the §9.3 pre-registration
contemplates freezing). Once ratified, this line is amended in place with
the date and the word RATIFIED, and the M1 run uses it verbatim.

## 5. Draft R3 acceptance criteria (REQUIRES RATIFICATION — the §9.3
pre-registration deliberately left the numeric thresholds open)

M1 = PASS requires ALL of:

1. Frozen-source (R1) realized PnL, date-clustered 95% CI lower bound > 0,
   σ≤4F filter applied PRE-selection, one entry per market.
2. `gefs` realized ≥ 0 on the same dates, OR an ex-ante disqualifier that
   demonstrably removes the loss (not merely the sample).
3. Beats the no-filter far-bracket-NO baseline on the same dates.
4. Walk-forward tail ratios within [0.8, 1.25] at |z|≥2.5 (R4).
5. Point estimate ≥ 4c/contract (below that, the 3c slippage sweep leaves
   no live execution margin).
6. ≥1 cold-season month of recorded ladders before any live sizing claim
   (R5 — may extend past Nov 7 for the final sign-off; the Sept–Oct verdict
   is provisional until R5 accrues).

Any single failure = HALT #3.

## 6. Standing risks and open items

- The maker-fee schedule question (secondary sources claim 25%-of-taker
  maker fees since 2026-07-07; live API on 2026-08-31 showed KXHIGH*
  `fee_type=quadratic`, maker $0.00). Re-verify live before ANY backtest
  that assumes a maker fee, in either direction.
- The sim settlement leg has never touched a live position; M2 closes that.
- Depth was never measured; it gates every live sizing claim.
- Gas reopening door (self-recorded gas ladders) closes further every
  month it isn't started; decision deferred to post-M1.
- `review_2026_07_24/` no longer exists on disk (gitignored, never
  committed). Its detail is recoverable only from the retained GCE boot
  disk — decide before deleting that disk.
