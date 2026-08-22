# VM decommission snapshot — 2026-08-22

Archive of the Google Compute Engine instance that ran Money Printer continuously
from 2026-03-22 to 2026-08-22, taken immediately before the instance was stopped
as part of the move to a local 24/7 host.

**The archive payload under `archive/` is deliberately untracked** (197 MB — see
the `.gitignore` entry at the repository root). This manifest, the checksums, and
the captured VM state under `meta/` **are** tracked, so the repository carries a
complete record of what was taken and how it was verified even where the bytes
themselves do not travel with git history.

---

## 1. Source instance

| field | value |
|---|---|
| instance | `money-printer-preschool-20260322` |
| zone / project | `us-central1-c` / `aerospaceaiagent` |
| machine type | `e2-standard-2` (2 vCPU, 8 GB) |
| host uptime at capture | 152 days |
| disk at capture | 29 GB, 93% used (2.2 GB free) |
| git checkout | branch `pivot_weather_v1` @ `9dcc78e`, clean but for 4 untracked files |
| workload | `run_web_dashboard.py --auto-cycle --sim-balance 3000` in tmux `money`, up 24 days |
| status after capture | **TERMINATED** (stopped 2026-08-22; boot disk retained) |

The instance ran **feed-only** throughout: `WEATHER_TRADING_ENABLED = False`. The
reconcile record confirms it independently — *"No weather position has ever been
opened."* Nothing in this snapshot represents executed trades against live capital.

Three cron jobs were active at capture and are recorded verbatim in
`meta/vm_state_2026-08-22.txt`: a 5-minute `host_watchdog.sh`, a daily
`settlement_reconcile.py` (06:00 UTC), and a daily `reconcile_weather.py --days 2`
(13:30 UTC).

---

## 2. Archive contents

| file | bytes | entries | what it holds |
|---|---|---|---|
| `market_csv.tgz` | 165,599,203 | 1,808 | **The primary asset.** Every unique `data_*.csv` (market snapshots) and `portfolio_*.csv` from `logs/` and `logs/_archive/`, 2026-01-27 → 2026-08-22 |
| `named_runs.tgz` | 18,551,264 | 429 | The 54 named (non-`cycle_*`) archive directories — labelled experiment runs such as `6hourRunData55to83PercentGOODRUN`, `train_50percent_ML_score86percent`, and the `startup_*` captures |
| `prod_logs.tgz` | 8,984,685 | 25 | Final production log (75,410,092 B, 24 days continuous), `recovered_prod_log_20260627_to_20260724.log`, `phase0_evidence/`, `host_watchdog.log`, `truth_norm.json`, `edge_test_vm.py`, `crontab_predeploy.bak`, and the VM-only untracked `scripts/analyze_journal.py` |
| `data_dir.tgz` | 7,883,279 | 474 | Complete `data/` tree: `weather_truth/` (incl. 27 daily reconcile runs), `forecast_archive/`, `ladders/`, `calibration/`, `models/`, `historical/`, `saved_runs/`, `exchange_state.json`, `trade_journal.jsonl`, `settlement_cache.json` |
| `hermes_config.tgz` | 2,808,095 | 3,383 | Hermes agent configuration, `SOUL.md`, skills, cron definitions, hooks, memories, logs |
| `session_logs.tgz` | 1,783,110 | 1,130 | Every per-cycle `session_*.log` narrative from the archive |

SHA-256 for each file: `meta/archive_sha256.txt`.

### Coverage of the market tape

Scanned from `market_csv.tgz` itself, not asserted (`meta/csv_coverage.txt`):

```
data_*.csv sessions       904
total rows         19,316,720
weather rows       12,783,852     174 distinct days, 2026-01-27 -> 2026-08-22
```

| series | rows | | series | rows |
|---|---|---|---|---|
| `KXHIGHCHI` | 2,367,672 | | `KXHIGHLAX` | 2,345,048 |
| `KXHIGHNY` | 2,366,599 | | forecast-tagged `(F)` variants | 3,304,854 |
| `KXHIGHMIA` | 2,364,813 | | unqualified `KXHIGH` | 34,866 |

Monthly weather-row volume rises from 35.7 k (Jan) to **5.66 M (Aug)** — the step
change at 2026-07 reflects the Phase 0 harvester hardening (FR-0.6: full ladder
depth with both bid *and* ask columns for all four cities). The August data is the
densest and most trustworthy segment; the pre-March data was captured by the
crypto-era harvester and carries the ladder-semantics caveats the Phase 1 work fixed.

Each row carries `Timestamp, Symbol, Price, Type, Status, Bid, Ask, NoBid, NoAsk,
Last, Volume, Depth, StrikeType, FloorStrike, CapStrike` — i.e. the API bracket
fields, not inferred direction. This is what any future weather backtest replays.

### Data of record that exists *only* here

`data/weather_truth/reconcile/` — **27 daily reconciliation runs, 2026-07-25 →
2026-08-21** — was deliberately untracked in commit `617dd66` ("untrack cron-written
reconcile outputs"). It was produced by the cron job on the VM and exists in no other
location. Of those, the runs from 2026-07-30 onward are **newer than any commit in
this repository**. See §5 for what they show.

Because it is both irreplaceable and small (54 files, 892 KB), this record is the one
part of the payload **committed to git** rather than left inside the ignored archive —
it is duplicated at `reconcile_record/` in this folder. The reason `617dd66` untracked
it was cron rewriting the files continuously on the VM; that churn ended when the
instance stopped, so the original rationale no longer applies. It is kept here rather
than restored to `data/weather_truth/reconcile/` so that the live-data path stays
untracked and a future harvester does not start committing over it.

---

## 3. What was excluded, and why

The instance held 13 GB. The snapshot is 197 MB because the bulk was mechanical
duplication rather than distinct information.

| excluded | raw | reason |
|---|---|---|
| 801 `cycle_*` archive dirs' copies of the production log | ~7.5 GB | Each 4-hour cycle boundary copied the *entire growing* `money_printer_20260729_040235.log`. All 801 are prefixes of one file; the final complete copy is in `prod_logs.tgz`. `host_watchdog.log` was likewise copied 801 times. |
| `logs/_archive/DebugData/` | 2.5 GB | Feb-2026 crypto-era debug logs (one 2.0 GB file, one 500 MB file) for strategies deleted in the Phase 0 teardown and adjudicated structurally unwinnable by the 2026-07-24 review. |
| `.hermes/hermes-agent/`, `.hermes/sessions/` | 1.3 GB | Node runtime (reinstallable) and agent chat history. Config, skills, and cron definitions were kept. |
| `venv/` | 2.2 GB | Reproducible from `requirements.txt`; the exact `pip freeze` is in `meta/vm_state_2026-08-22.txt`. |
| 448 duplicate CSVs | ~80 MB | Same basename appearing in several cycle dirs as the file grew; the largest (most complete) copy of each of the 1,808 unique basenames was kept. |
| `.env`, `.hermes/.env`, `.hermes/auth.json`, private keys | — | **Secrets.** A redacted rendering of the application `.env` is in `meta/vm_state_2026-08-22.txt`. See §6. |

The CSV deduplication rule was: group by basename, keep the copy with the largest
byte count. The resulting file list is preserved verbatim at
`meta/csv_dedup_filelist.txt` (1,808 paths), so the selection is auditable rather
than merely asserted.

---

## 4. Verification record

Every archive passed `gzip -t`, and entry counts were matched against counts taken
independently on the VM (`data/` 474 = 474; unique CSVs 1,808 = 1,808; session logs
1,130 = 1,130).

Content was then verified by SHA-256 on five representative files spanning every
archive — comparing the VM's own digest against the digest of the file extracted
back out of the snapshot:

| file | result |
|---|---|
| `data/weather_truth/reconcile/reconcile_2026-08-21.json` | match |
| `data/exchange_state.json` | match |
| `data/trade_journal.jsonl` | match |
| `logs/_archive/cycle_20260821_001911_dd754/data_20260820_201858.csv` | match |
| `logs/money_printer_20260729_040235.log` | match *(see below)* |

The production log initially mismatched because it was still being appended to by
the running process. It was confirmed to be a byte-exact **prefix** of the live file
(local 75,403,838 B hashed identically to the VM's first 75,403,838 B), then the
trading process was stopped with `SIGTERM`, the log finalised at 75,410,092 B, and
the archive re-pulled. The final copy matches the VM's final digest
`e472729c65fb8b11d67ae999e99180f1d926fed2ab679b52606c325b49ebccbd` exactly.

The VM was stopped only after all five checksums matched.

---

## 5. What the newest data shows

The 27 reconcile runs cover **112 distinct city-days across KNYC / KMDW / KLAX / KMIA**:

```
markets checked   1296
outcomes verified 1188
matched           1188
unexplained          0
```

Every non-match is the single explained category `NO_RESULT` (108 rows: Kalshi
marked the market `closed` without publishing a yes/no result). **Zero unexplained
mismatches across the entire 27-day record.**

This is the strongest evidence the project holds that the Phase 1 bracket-settlement
semantics — the `strike_type` / `floor_strike` / `cap_strike` → payoff rule that
replaced the inverted suffix-letter parser — are correct. It was accumulated *after*
the Phase 2 HALT, by machinery that kept running while no one was watching, and it
validates the layer underneath both halted strategies.

Note the reconcile report's own caveat, which this manifest does not paper over:
the sim leg is `NOTHING TO CHECK` — 1,050 sim records loaded, none for a reconciled
market, because no weather position has ever been opened. Settlement *semantics* are
verified against live data; the settlement *path through the simulator* is covered
only by unit tests.

---

## 6. Secrets — action required

The VM's `.env` held live credentials: `ANTHROPIC_API_KEY`, Kalshi read-only key ID
and private key path, Coinbase API and secret keys, and a Discord webhook URL. **No
secret value is stored in this snapshot** — the `.env` files, `auth.json`, and the
`.key` files were excluded from every archive, and the copy in
`meta/vm_state_2026-08-22.txt` is truncated to the first 6 characters of each value.

The boot disk still exists in stopped state and still contains those plaintext
credentials. **Rotate the `ANTHROPIC_API_KEY` and the Discord webhook before deleting
the disk, and treat them as exposed until you do.** The Kalshi key is read-only, and
Coinbase is a placeholder, but both are worth cycling on the same pass.

---

## 7. Restoring

```bash
cd vm_snapshot_2026_08_22
sha256sum -c meta/archive_sha256.txt        # verify before trusting
mkdir -p extracted && for f in archive/*.tgz; do tar xzf "$f" -C extracted; done
```

Paths inside the archives are relative to the VM's `/home/hoyer`, so they extract as
`extracted/money_printer/...`, `extracted/data/...`, `extracted/.hermes/...`, and
`extracted/phase0_evidence/...`.

`extracted/` is git-ignored and fully regenerable from the archives.

Do **not** copy the reconcile outputs back into `data/weather_truth/reconcile/` — that
path is untracked by design so a future harvester can write to it freely. The committed
copy at `reconcile_record/` is the archival one; read it there.
