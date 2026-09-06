# External review handoff — Money Printer, 2026-09-06

Written for an outside agent with more capability than the team that built this, and a
short window. You are being asked one question, and given everything needed to answer it
honestly. Read this file first, then `HANDOFF.md`, then `PRD_STRATEGY_FACTORY.md`. Do not
start from the code.

## 0. The question

**Is the strategy-factory approach worth continuing, and if so what is the highest-value
next move? If not, what is the more profitable direction, with evidence at the same standard
this repo holds itself to?**

"Validate us" is not the goal. The goal is a correct answer. A well-argued "stop, do X
instead" is worth more than a polite "looks fine". Two prior engines were killed by this
project's own process (HANDOFF §1); a third being killed would not be a failure of the
review.

## 1. What this is, in one paragraph

An algorithmic trading system for Kalshi event contracts (weather daily-high ladders,
`KXHIGH*`). It has never made money. Its real asset is an evaluation process that has
refused to lie to itself: settlement semantics reconciled against external truth (1,188/1,188
markets, 0 unexplained), sealed out-of-sample data roots, pre-registered gates, and verdicts
that were allowed to be "no". The current bet (2026-09) is an evolutionary **strategy
factory**: rule genomes over a frozen feature frame, realized-settlement fitness (never
modelled EV), sealed holdouts, and a paper gate on the sandbox before any capital. Capital at
risk if it ever passes: about $350.

## 2. Where it stands today (all numbers are in the repo, cited)

| Item | State | Where |
|---|---|---|
| Family #1 `weather/gfs_mex/taker/v1` | **CLOSED** — pooled OOS +0.0308/contract, boot CI [−0.090, +0.142], `p_RC` 0.886, Holm p 0.29 | `reports/factory/registry.jsonl`, HANDOFF 2026-09-04 entry |
| Search reproducibility | 96k-evaluation search reproduces byte-identically across x86-64 and aarch64 | HANDOFF 2026-09-06 |
| Deployed genome `0c4b20502f2daf65` | shadow on maia (Pi 4); emits, never trades; **117/130 of its offline trades size to 0** under the sandbox's cold-start Kelly rule | PRD Phase F4 registered deviation; `reports/factory/sizing_cold_start_2026-09-05.md` |
| Calibration transfer gap | closed: bot now serves the frame's walk-forward calibration; parity 0 discrepancies; archives pinned in the spec hash | PRD §8 + Phase F4 block |
| Fill realism | next-poll p90 drift 0.00 on the genome's own trading hours (n=156) | `reports/factory/fill_realism_2026-09-06.md` |
| Phase F4 machinery | built and adversarially verified; unseal/score/gate refuse today for want of a PROPOSED genome | PRD Phase F4 block; `docs/FACTORY.md` |
| Ratification | `docs/REVIVAL_2026_09.md` RATIFIED 2026-09-06 (R1 source freeze, R3 thresholds); owner decisions 11–13 taken | PRD §9 |
| Sealed roots | holdout-B `data/ladders_2026-09`-style roots are **sealed**; prices never searched. **Outcome labels leak** via `manifest.json`/`RECONCILE.md` (recorded) | HANDOFF 2026-09-05 correction |
| Next planned step | `weather/gfs_mex/taker/v2`: fold the runtime sizing law into `sandbox_admissible`, re-search; expected outcome CLOSED (survivors realize ≈ −0.048/contract unfiltered) | HANDOFF 2026-09-05 entry, PRD decision 6 |

## 3. What the evidence actually supports (read this before forming a view)

- The weather signal is not nothing: under `gfs_mex` a hand-specified shape realized
  +6.36¢/contract over 181 trades with CI excluding zero, sign stable across 4 cities and 3
  months (`reports/phase2/phase2_go_no_go_2026-07-26.md`). It was HALTed because the sign
  reversed under a different forecast source and the gate quantity ranked the loser higher.
- The factory found nothing better than that hand rule on 69 development dates, and the
  hand rule itself does not survive multiplicity correction.
- Power is the binding constraint: ~37 holdout dates cannot separate a real 3–5¢ edge from
  noise (PRD §9 risk 1). The minimum detectable effect on holdout-B is ≈ +0.088/contract,
  2.9× the point estimate.
- Everything promoted so far is unexecutable at cold start in the sandbox (sizing ceiling).
- Both HALTs shared one failure mode: modelled EV was optimistic and the market price was
  the better forecaster. Any new idea evaluated on modelled EV will fail the same way.

## 4. What you are free to challenge

Everything, including: the choice of market (weather ladders at $350 capacity), the fitness
definition, the gene space (GENE_SPEC v1 is coarse; v2 with lagged quote genes is designed but
unbuilt, `docs/factory/FACTORY_ARCHITECTURE.md`), the independence unit (`target_date`,
which merges 4 cities into one unit and costs power), the sizing gauntlet (owner rejected
editing `src/core/risk_manager.py` — PRD decision 10; you may argue it, not silently change
it), and the decision to keep searching weather at all versus the feed-only harvesters
(`mention`, `crypto_annual`, `tweets`, gas) whose screening is in
`docs/MARKETS_EXPANSION_2026_09.md`.

## 5. What you may not do

These are the reasons the verdicts here are trustworthy. Breaking them makes your output
unusable, however good it is.

1. **Do not read prices or labels from sealed roots** (`data/ladders_holdout/`,
   `data/ladders_2026-09/`, `data/quarantine/`). Not "just to look". The loaders refuse;
   `git show` does not. If you design anything after reading them, say so in writing.
2. **No live capital, no live orders.** Every credential is read-only; keep it that way.
   `tests/test_factory_no_live_capital.py` and `tests/test_protected_files.py` must stay green.
3. **Do not edit protected files** (`src/core/*`, `src/bots/mixins.py`) without a written
   ratification entry; do not loosen risk rules to make a specific genome trade.
4. **Realized settlement outcomes only, clustered on the independent unit.** Modelled EV is
   diagnostic, never a score (HANDOFF §3 rules 1–5).
5. **Register, never re-scope.** If a criterion cannot be met, write the deviation beside it
   (HANDOFF §3 rule 9). Do not edit a pre-registered threshold after seeing a result.
6. **A documented "no" is a valid deliverable** (rule 6).
7. Do not push to `main`; work on a branch named `review/astra-2026-09-06`.

## 6. How to work here

```bash
pip install -r requirements.txt          # dev box lacks xgboost/websocket-client -> 17 known test failures
$env:PYTHONPATH = "."                    # Windows; scripts import src.*
python -m pytest tests/ -q -p no:cacheprovider --continue-on-collection-errors --ignore=tests/test_web_dashboard.py
python scripts/factory.py --help         # board | coverage | freeze-frame | gen0 | run | controls | report | holdout | score | promote | register-gate | gate
python scripts/factory.py board --paper-url http://maia.local:8050
python scripts/factory.py holdout --audit
python scripts/factory_replay_parity.py --only fr31a_taker --calibration live --out /tmp/p.json
```

Machines: **alcyone** (DGX Spark, aarch64, the lab; `ssh alcyone`, checkout
`~/projects/money_printer`, runs the daily ladder capture and the weekly reconcile timer;
never force-reset it, it carries local data commits) and **maia** (Pi 4 sandbox,
`http://maia.local:8050`, read-only HTTP from the dev box; deploy via
`deploy/pi/deploy_f3_shadow.sh` on maia itself). The frozen frames are gitignored:
`data/factory/frames/weather_2026-07-25_bfcf94654a3a` (dev box + alcyone).

Design record: `docs/factory/FACTORY_ARCHITECTURE.md` (the spec), `FACTORY_ROADMAP.md`
(phases F0–F5 with falsifiable exit criteria), `F2_RUNBOOK.md`/`F3_RUNBOOK.md`,
`docs/FACTORY.md` (operator runbook). Governance: `PRD_STRATEGY_FACTORY.md` §8 (per-criterion
status with every registered deviation) and §9 (owner decisions 1–13).

## 7. What a useful deliverable looks like

One document, `reports/review/astra_2026-09-06.md`, with:

1. **Verdict**: continue / redirect / stop, in one sentence, then the argument.
2. **The single most important thing the team is wrong about**, with the evidence from this
   repo that shows it. If you find none, say what you looked for.
3. If continue: the concrete next experiment, pre-registered — hypothesis, data root it may
   touch, the unit, the threshold, the sample size and its power, what "no" looks like.
4. If redirect: the alternative, its capacity at $350, its fee arithmetic, the settlement
   truth source, and the first falsifiable test — held to §5 above.
5. Anything you built: on the review branch, tests green, protected diff empty.

Numbers in the document must be reproducible from a command you name. If it cannot be
reproduced from this repo tonight, mark it as a claim, not a finding.

## 8. Known traps (each cost a day)

- `_parse_price` returns 0.0 for a MISSING ask; a zero ask is an empty book, not a quote.
- A long-running collector analyses with the module it imported at launch; re-run analyses
  from the tape at HEAD.
- `git show HEAD:` is LF on this box while the working tree is CRLF; hash CRLF-normalised.
- Worktrees created here may start at a stale commit; check `git log -1` before working.
- The full test suite needs `--continue-on-collection-errors` and Git Bash first on PATH.
- The NO-side settlement sign bug (fixed 724d93c) means any sandbox state written before
  2026-09-05 was repaired; do not re-derive PnL from pre-repair journals.
