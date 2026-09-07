**Astra Seeded status — September 6, 2026**

Branch: `astra_seeded`, created from the reviewed project at `230ffe4`.
Active specification: [PRD_ASTRA_SEEDED.md](../PRD_ASTRA_SEEDED.md).

**Completed:** researched PRD; offline Brier coordinate targets and rebalance deltas; complete YES/NO basket screening with depth, cash, freshness and conservative fee allowances; synthetic CLI example; failure-case tests. No exchange, forecast API or paid service is called.

**P0 is partially complete.** Numerical primitives are implemented. Exact published live sizing/prompt details and an independently auditable source ledger remain unresolved. The primitives are not a completed replication of the reported trading agent.

```powershell
python scripts/astra_seeded.py examples/astra_seeded/synthetic.json
python -m unittest tests.test_astra_seeded -q
```

The CLI accepts explicit files only under `examples/astra_seeded/` or `data/astra_seeded/development/`. It produces research candidates, not orders or fills. Book inputs are already normalized; it is not yet a Kalshi API client. Each returned basket size is an alternative, not an additional order to sum with other sizes.

Brier outputs are signed per-outcome coordinates. Converting negative coordinates to NO purchases, combining economically equivalent positions, enforcing caps and avoiding duplicate binary exposure belongs in the P1 adapter. Never directly submit these vectors as orders. The scale in the example is synthetic and does not reproduce the source deployment's sizing.

The basket certificate is a trusted-input assertion about settlement rules. The scanner checks membership and flags, not the rules against Kalshi. Its surplus assumes all legs eventually fill at the modeled costs and ordinary stated settlement occurs. The cent-rounded screening model is conservative under its assumed fills; it is not the account-specific fee accumulator.

**Next bounded task:** finish source-replication gaps, then P1 account ledger/risk/execution states with partial-fill and restart tests. Implement forecast ingestion and prospective recording after the cost budget is specified. No live capital, maker quotes, artificial profit series or paid inference are enabled.

**Validation:** 18 standalone tests pass. Combined pytest run: **121 passed, 15 subtests passed** in 5.24 seconds, including protected files and factory no-live-capital tests:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='.'
python -m pytest tests/test_astra_seeded.py tests/test_protected_files.py tests/test_factory_no_live_capital.py -q -p no:cacheprovider --disable-warnings --tb=short
```

Protected source diff against `230ffe4` is empty. No full-suite certification is claimed. The earlier review artifacts in `reports/review/` are included with this handoff for context; they preceded the new strategy track.

**Fable handoff — September 7, 2026**

**Live-check update:** see [Pleiades host-health findings](../reports/review/pleiades_health_2026-09-07.md). Both hosts, inference and fresh market feeds work. Maia's state is actually on microSD, with no attached SSD, and Docker memory controls are unsupported. Prefer Alcyone's existing NVMe for the initial write-heavy Astra service or resolve Maia's storage/controller setup first. The documented-topology assumptions below are superseded by that report where they differ.

The owner requested this branch be committed and pushed for continuation. Read this status and the active PRD first; the earlier review is reference material. Source files are `src/astra_seeded/methods.py`, `scripts/astra_seeded.py`, and `tests/test_astra_seeded.py`. Preserve old data and protected code. Do not treat the synthetic CLI as an already working paper trader.

The documented Pleiades topology (`deploy/README.md` and the sibling `pleiades/README.md`) has Alcyone (DGX Spark) for inference/research, Maia (Pi 4) for the Docker sandbox/dashboard, and Electra (Pi 5) reserved. Reuse Maia with SSD-backed state for an initially modest market universe; Alcyone can supply forecasts through the cluster's permitted interface. A local forecaster is a separately evaluated adaptation, not the paper's Gemini result. Do not expose an Alcyone model server on the LAN contrary to the existing trust boundary; use the approved bridge or have Alcyone publish forecast records.

Engineering estimate, conditional on working Docker/LAN/read-only credentials and an available forecast source: **1–2 focused development days** for an initial live-feed paper simulation and status dashboard; **3–5 days elapsed** for a dependable first deployment including a **24–48-hour soak**. This is a scope estimate, not a benchmark or deadline. Live host health was not checked for this handoff. Forecast access or unresolved replication details may extend it; validating profitability takes the prospective period in the PRD, not a few days.

Suggested first delivery order:

1. Finish P0 replication-gap register and build P1's persistent account ledger, cash reservations, bounded exposure, signed-coordinate conversion, and order state machine. Prove YES/NO payoff signs and pending-order deduplication.
2. Connect the read-only market provider and timestamped forecast records; implement conservative taker fills at observed depth, fees, settlement reconciliation and restart recovery. Run only S1 initially. Keep missing/stale inputs explicit and skip trading on them.
3. Add a small status API and adapt the existing dashboard components for equity, cash, positions, signals/rejections, fills, net PnL, costs and feed health. Existing `src/web/server.py` endpoints show the previous interface; keep the new account's data isolated. Paper PnL must be driven by the new ledger, not the old simulator's balances.
4. Deploy a separate paper service on Maia with its own state directory and a confirmed unused LAN port; retain the legacy collector on :8050. Reuse Docker health/restart patterns and SSD storage. Select host paths/port after inspection; do not reset an active host checkout.
5. Soak through feed interruption, restart, duplicate messages and partial fills; compare accounting to independent settlement truth. Expect actual settlement evidence to accumulate over the selected markets' horizons. Add S2 discovery and S3 only in their PRD order.

MVP acceptance: dashboard accessible on LAN; $300 initial paper balance; fresh market/forecast timestamps; real decisions and explicit rejection reasons; depth-limited simulated fills; fees and reserved cash visible; settled trades reconciled; restart retains state; no live-order submission path. Reusing UI components and deployment patterns is encouraged; reusing the old sizing or simulated-profit assumptions is not.
