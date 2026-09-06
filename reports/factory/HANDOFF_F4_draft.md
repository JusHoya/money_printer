# HANDOFF.md section 8 entry -- Phase F4 verdict (DRAFT SKELETON, not yet in HANDOFF.md)

> Fill every `<...>` from the artifacts named beside it; delete the branch that
> did not happen. FR-F4.2 requires the entry **either way** (HANDOFF section 3
> rule 6: a documented HALT satisfies the exit criterion). Do not commit this
> file into HANDOFF.md as-is -- it is the orchestrator's template.

### <YYYY-MM-DD> -- Phase F4 verdict: `<family>` genome `<genome_id>` is <PASS | HALT | CLOSED-unscored>

**What was scored, once each.**
- Holdout-B (`data/ladders_holdout`, 2026-07-26..08-31): unseal `RATIFIED-<date>`,
  `<n_finalists>` finalists, Holm-adjusted p `<p_adj>` for `<genome_id>`;
  `reports/factory/unseal_log.jsonl` line `<n>`. Truth filter dropped `<pct>` % of
  city-days (< 10 % required). Seal caveat recorded: `<yes/no -- the manifest.json
  result labels and RECONCILE.md were/were not read before the unseal>`.
- R3 (`data/ladders_2026-09`, 2026-09-01..<last date>, `<n_city_days>` city-days,
  2026-08-31 quarantined): result sha256 `<sha256>` printed before the numbers;
  registry line `<PASS | HALT #3>`; realized `<c/contract>` [`<lo>`, `<hi>`].
- Holm across `<k>` registered families: `<p_adj holdout-B>` / `<p_adj R3>`
  (both < 0.05 required for promotion).

**Promotion commit.** `<commit sha>`: `configs/factory/promoted/<genome_id>.json`
(`spec_hash <12>`, mode `<shadow|paper>`, `calibration.kind <walk_forward|frozen>`),
`configs/factory/gate_registration.json` (`registration_commit_utc <ISO>`, from
`git log --diff-filter=A`), `GENOME_STRATEGY_ID=<genome_id>` on maia since
`<ISO>` (`deploy_f3_shadow.sh`, boundary `<:00Z | --any-time, day forfeited>`).
`ML_WEATHER_ENABLED=False` confirmed by the loaded line `<quote>`.

**Paper record (never equity).** Settlement within 3 days: `<PASS|FAIL>`
(`scripts/check_settlement_latency.py`, `<n>` settled KXHIGH positions, max gap
`<d>` d, `<n>` overdue open) -- per strategy: V2 `<n>/<n>`, ML-Weather `<n>/<n>`,
`Genome <id8>` `<n>/<n> | NO EVIDENCE (shadow books nothing)`. Weekly reconcile
(`reports/factory/paper_reconcile_<from>_<to>.json`, `<n>` weeks): sandbox
trade set ⊆ lab `<yes|no>`, unexplained `lab_only` `<n>`, sign mismatches `<n>`,
frame `<sha12>` `<= spec | fallback, see FACTORY.md 4.8>`.

**Board row (verbatim).**

```
| PAPER | <mode k/n_min | KILLED:reason> | <family> | `<id8>` Genome <id8> (<mode>) | sandbox <c/contract> (<n> fills) | <k>/50 target_dates | <n> | — | — | — | pred <c/contract> [<lo>, <hi>] (<source>) | — | — | <note> |
```

**Gate (`reports/factory/gate_<genome_id>.json`).** `grouped_count <k>` (>= 50),
`p_exact <p>` (< 0.05; exact Poisson-binomial over `target_date` units,
least-favourable within-unit null; `<n>` units lost to cross-city merging),
`net_pnl <$>` (> 0), `spec_hash_unchanged <true|false>`, `fee_type_matches
<true|false>`, `excluded_rate <r>` (<= 0.02), `realistic_fills_enabled
<true|false|REFUSED>` (source `<fill_config.jsonl sha12 | operator assertion>`),
`registered_before_first_trade <true|false>` (first fill `<ISO>` vs registration
`<ISO>`). Verdict: **`<PASS | FAIL | REFUSED>`**.

---- EITHER WAY, keep exactly one of the two blocks below ----

**If PASS.** The family moves to `<RATIFIED-for-capital?>` only by a separate
owner decision; nothing here clears live capital (`read_only=True` stands;
`tests/test_factory_no_live_capital.py` green at `<commit>`). What the number
does NOT say: `<the registered deviations that still apply -- calibration
provider, fill realism 20-s window, cross-city merging, n_min power>`.

**If HALT.** Registry transition `HALT` appended at `<commit>` (terminal; a rerun
is `<family>/v2`). The board reads `KILLED:<HALT|GATE_FAIL>`;
`GENOME_STRATEGY_ID` removed from maia's `.env` at `<ISO>`; the waterfall is
exactly what it was. `tests/test_factory_no_live_capital.py` green at `<commit>`
(no file under `src/factory/` references a live-capital flag). Which condition
failed and by how much: `<condition: observed vs required>`. What was learned
that a v2 must encode: `<one paragraph>`.

**Still open after this entry.** `<numbered list, or "nothing -- the family is
closed and the next phase is <F5 | v2 re-search | nothing until data>">`.
