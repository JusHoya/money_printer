# F2 runbook — the family #1 evolutionary run on alcyone

Phase F2 of `PRD_STRATEGY_FACTORY.md` (FR-F2.1–F2.6; exit criteria in the
"Phase F2" block) and `docs/factory/FACTORY_ROADMAP.md` §F2. Design record:
`docs/factory/FACTORY_ARCHITECTURE.md` §7 (compute plan), §10 (Hermes/Discord).
Everything below runs **ON alcyone** as `jushoya` from `~/projects/money_printer`
unless it says otherwise. Nothing here touches maia or live capital.

What the run produces and where (F2 sprint contract):

| path | tracked | written by |
|---|---|---|
| `data/factory/runs/<run_id>/{run.json,folds.json,status.json,picks.json,ledger/,oos/,controls/}` | no (`data/factory/` ignored) | `factory.py run` / `controls` |
| `reports/factory/<run_id>/{summary.json,summary.md,oos_by_date.csv,finalists.json,board.md,status.json,run.json}` | **yes** | `factory.py report` |
| `reports/factory/<run_id>/bench.json` | **yes** | `scripts/factory_bench_coexist.py` (idle / running / compare) + the wrapper (throughput, host numbers) |
| `reports/factory/<run_id>/completion.txt` | **yes** | `deploy/spark/mp_factory_notify.sh` |
| `reports/factory/<run_id>/resources.log` | no | the wrapper's 60-s `free -g` sampler |
| `reports/factory/latest.json` (`active_run`, `status`, `run`, `board`) | **yes** | EVOLVE at run start, STATS `report` at the end |
| `reports/factory/registry.jsonl` (PROPOSED / CLOSED transition line) | **yes** | STATS `report` |

The tools, one line each: the **user unit** `mp-factory@<run_id>` runs the
**wrapper** `deploy/spark/mp_factory_run.sh`, which runs `factory.py run →
controls → report` inside the network-less `factory` compose service, samples
memory, merges the throughput into `bench.json`, then calls the **notifier**
`deploy/spark/mp_factory_notify.sh` (writes `completion.txt`, tries `hermes send`).
The **monitor cron** `mp_factory_monitor.sh` (10 min, no agent) posts one
progress line per `status.json` change and `completion.txt` once. The
**bench** `scripts/factory_bench_coexist.py` samples mp-vllm on the host before
and during the run and computes the ≤10 % verdict.

---

## 0. Preconditions (check, do not assume)

```bash
cd ~/projects/money_printer
git status --short                      # clean; the factory refuses "+dirty" revs on the report
docker image inspect money-printer-lab:latest >/dev/null && echo image-ok
ls data/factory/frames/                 # weather_2026-07-25_bfcf94654a3a (frozen frame; else `factory.py freeze-frame`)
free -g                                 # 2026-09-03: total 121, used 27, available 93 with mp-vllm + 2 hermes containers
curl -s http://127.0.0.1:8000/v1/models | head -c 300; echo   # mp-vllm serving ykarout/Qwen3.5-9B-NVFP4
id -nG | tr ' ' '\n' | grep -x docker   # jushoya must be in the docker group
```

## 1. Fast-forward the checkout

```bash
cd ~/projects/money_printer
git fetch origin
git checkout sprint/f2-evolution            # or main once the F2 merge has landed
git pull --ff-only
# only if Dockerfile.lab / requirements changed since the image was built:
docker compose -f deploy/spark/docker-compose.lab.yml build
docker compose -f deploy/spark/docker-compose.lab.yml run --rm factory python scripts/factory.py --help | grep -E 'run|controls|report'
```

## 2. Bind directories (the compose file cannot create them — see its header)

```bash
mkdir -p logs data/factory data/factory/runs data/ladders_holdout data/ladders_2026-09 reports/factory
```

## 3. Install the user unit

```bash
bash deploy/spark/install_factory_unit.sh     # copies mp-factory@.service into ~/.config/systemd/user, daemon-reload
loginctl enable-linger jushoya                # once; otherwise the unit dies with the SSH session
systemctl --user cat mp-factory@.service | head -3
MP_FACTORY_DRY_RUN=1 bash deploy/spark/mp_factory_run.sh run_dry   # prints the plan, touches nothing
```

`alcyone` had **no** user units before F2 (`~/.config/systemd/user` empty), so
this is the first one; `systemctl --user list-units 'mp-*'` should now list
nothing running and `systemctl --user list-unit-files 'mp-*'` the template.

## 4. Install the monitor cron (10 min, no agent)

```bash
cp hermes_plugin/scripts/mp_factory_monitor.sh ~/.hermes/scripts/
~/.local/bin/hermes cron create 10m --name mp-factory-monitor --no-agent \
    --script mp_factory_monitor.sh --deliver discord:1491982736989093961 \
    --provider custom --model ykarout/Qwen3.5-9B-NVFP4
~/.local/bin/hermes cron list                 # Mode: no-agent, Script: mp_factory_monitor.sh, every 10m
grep MONEY_PRINTER_FACTORY_DIR ~/.hermes/.env # must be /home/jushoya/projects/money_printer/reports/factory
```

The existing hourly `mp-factory-board` cron stays; it posts `board.md` when the
report lands. Both scripts self-hash (`~/.hermes/state/mp_factory_monitor.sha`,
`...board.sha`) because `--monitor-script` is incompatible with `--no-agent`.

Dry-run the monitor by hand with a throwaway state file (no Discord involved):

```bash
MP_FACTORY_MONITOR_STATE=/tmp/mon.sha bash ~/.hermes/scripts/mp_factory_monitor.sh   # prints the line (or nothing before a run)
MP_FACTORY_MONITOR_STATE=/tmp/mon.sha bash ~/.hermes/scripts/mp_factory_monitor.sh   # silent: same sha
```

## 5. Pick the run id and take the IDLE bench

```bash
RUN_ID=run_$(date -u +%F); echo $RUN_ID
mkdir -p reports/factory/$RUN_ID
python3 scripts/factory_bench_coexist.py --label idle --n 12 --max-tokens 96 \
    --endpoint http://127.0.0.1:8000/v1 --model ykarout/Qwen3.5-9B-NVFP4 \
    --out reports/factory/$RUN_ID/bench.json
```

Take it while the factory is NOT running and no Hermes turn is in flight
(`docker stats --no-stream` shows the hermes containers idle). It prints the
p50/p90 inter-token ms, p50 TTFT and tok/s and writes `mp_vllm.idle`.

## 6. Start the run and watch it

```bash
systemctl --user start mp-factory@$RUN_ID
journalctl --user -u mp-factory@$RUN_ID -f                 # the wrapper's [mp-factory ...] lines + factory.py output
systemctl --user status mp-factory@$RUN_ID --no-pager
cat data/factory/runs/$RUN_ID/status.json                  # state RUNNING, phase, campaign, gen/n_gens, best_fit, evaluations
tail -3 reports/factory/$RUN_ID/resources.log              # t=<s> used_gib=<n> avail_gib=<n> phase=run|controls|report
```

Expected wall: real run (4 campaigns + 5 blocked folds ≈ 216k evaluations at
~6.6k evals/s ≈ minutes) then 41 control replicates (≈ 3M evaluations, tens of
minutes), then the report — well under the 4 h ceiling (§7.2). The unit's
`TimeoutStartSec=6h` only stops a wedged run.

**Failure / restart.** The unit is `Restart=on-failure` (5 tries per hour). The
wrapper sees `data/factory/runs/$RUN_ID/run.json` and re-runs `factory.py run
--run-id $RUN_ID --resume` (EVOLVE resumes at the last generation whose rows are
all SCORED); `controls` is resumable per replicate. Each failure writes a FAILED
`completion.txt` that the monitor posts once; the final DONE one replaces it. To
restart by hand: `systemctl --user start mp-factory@$RUN_ID` again. If EVOLVE's
CLI spells the resume flag differently, set it once in the unit:
`systemctl --user edit mp-factory@.service` → `[Service]`
`Environment=MP_FACTORY_RESUME_ARGS=--resume`.

**Smoke first (recommended).** A short run with a small budget and no controls
exercises the whole path (unit → wrapper → notify → monitor) in minutes:

```bash
MP_FACTORY_SKIP_CONTROLS=1 MP_FACTORY_RUN_ARGS="<EVOLVE's small-budget flags, e.g. --population 40 --generations 3>" \
    bash deploy/spark/mp_factory_run.sh smoke_$(date -u +%F)
```

Do not commit a smoke run's report directory; delete it (`reports/factory/smoke_*`)
and reset `latest.json` (`git checkout -- reports/factory/latest.json`) before
the real run so the monitor follows the right `status` pointer.

## 7. Mid-run: the RUNNING bench

While `status.json` says `"state": "RUNNING"` and the 16 workers are busy
(`docker stats --no-stream` shows the factory container near 1600 % CPU):

```bash
python3 scripts/factory_bench_coexist.py --label running --n 12 --max-tokens 96 \
    --out reports/factory/$RUN_ID/bench.json
```

Same prompt, same model, same N; writes `mp_vllm.running`. The factory never
touches the GPU and sits on the 16-core cpuset at `nice 10`, so the expected
change is small; take it during `phase=run` or `phase=controls`, not `report`.

## 8. Compare

```bash
python3 scripts/factory_bench_coexist.py --compare --out reports/factory/$RUN_ID/bench.json
# exit 0 = PASS (|Δ p50 inter-token| ≤ 10 %), 3 = FAIL, and the numbers are in mp_vllm.compare
```

## 9. After completion

```bash
systemctl --user status mp-factory@$RUN_ID --no-pager     # inactive (dead), status=0/SUCCESS
cat reports/factory/$RUN_ID/completion.txt               # run_id, state, verdict, pooled OOS, wall_s, used_gib before/after/peak
python3 -c 'import json;b=json.load(open("reports/factory/'$RUN_ID'/bench.json"));print(json.dumps({"compare":b["mp_vllm"].get("compare"),"factory":b.get("factory")},indent=1))'
awk -F'used_gib=' '/used_gib=/{split($2,a," "); if(a[1]+0>m)m=a[1]+0} END{print "peak used GiB", m}' reports/factory/$RUN_ID/resources.log
tail -2 reports/factory/$RUN_ID/resources.log            # "# end ... wall_s=" and "# used_gib before= after= peak="
cat reports/factory/$RUN_ID/summary.md | head -60
tail -1 reports/factory/registry.jsonl                    # the PROPOSED / CLOSED transition line
```

Exit-criterion mapping: **< 4 h wall** = `wall_s` in `completion.txt` /
`resources.log`; **`free -g` used ≤ 40 GiB** = `used_gib peak` (sampled every
60 s while mp-vllm and Hermes kept running); **mp-vllm p50 change ≤ 10 %** =
`bench.json` → `mp_vllm.compare.pass`; **throughput** = `bench.json` →
`factory.throughput` (evaluations / wall_s, from the run's `status.json` /
`run.json`) beside the gen-0 figure (6,589 evals/s on 16 workers).

## 10. Discord evidence (≥3 monitor posts + one completion message)

- The monitor cron posts one line per `status.json` change it sees at its
  10-min cadence (`factory <run_id> RUNNING evolve A gen 12/60 best_fit +0.0123
  phenotypes 812 evals 4800`), so a run longer than 30 min yields ≥3 posts; the
  completion post is `completion.txt` (once), and `mp_factory_notify.sh` sends
  the same text directly with `hermes send` when the run ends.
- Evidence of record is the channel history in `discord:1491982736989093961`
  (screenshot or copy the ≥3 progress lines and the `[factory] <run_id> DONE`
  message into the sprint report). On the host the same posts are recoverable
  from the cron's state files (`~/.hermes/state/mp_factory_monitor.sha` holds
  the sha of the last posted status.json; `...sha.completion` the completion
  sha) and from Hermes' cron log under `~/.hermes` (`~/.local/bin/hermes cron
  --help` lists the runs/log subcommand on the installed version; the
  no-agent job's stdout is what got delivered). The notifier's own delivery
  result is in the unit journal: `journalctl --user -u mp-factory@$RUN_ID | grep -E 'delivered|hermes'`.
- If the cron posted nothing: `~/.local/bin/hermes cron list` (job present,
  no-agent), `grep MONEY_PRINTER_FACTORY_DIR ~/.hermes/.env`, and run the script
  by hand with a throwaway state file (step 4) — non-empty stdout means the
  cron would have posted.

## 11. What to commit (explicit paths only — never `git add -A`)

```bash
git add -- reports/factory/$RUN_ID/summary.json reports/factory/$RUN_ID/summary.md \
           reports/factory/$RUN_ID/oos_by_date.csv reports/factory/$RUN_ID/finalists.json \
           reports/factory/$RUN_ID/board.md reports/factory/$RUN_ID/status.json \
           reports/factory/$RUN_ID/run.json reports/factory/$RUN_ID/bench.json \
           reports/factory/$RUN_ID/completion.txt \
           reports/factory/latest.json reports/factory/registry.jsonl
git status --short                          # nothing else staged; gen_*/controls/resources.log/frames are ignored
git commit -m "data(factory): family #1 run $RUN_ID -- <PROPOSED|CLOSED>, pooled OOS <mean> [<lo>,<hi>], bench pass=<true|false>"
git push origin HEAD
```

Do **not** commit anything under `data/factory/` (ledgers, controls, frames),
`resources.log`, or a smoke run's directory. `git check-ignore -v <path>`
answers any doubt.

## 12. Cleanup / repeat

- A second run on the same day needs a new id (`run_2026-09-04b`); the wrapper
  resumes an existing id rather than starting over.
- `systemctl --user stop mp-factory@$RUN_ID` stops a run; the next `start`
  resumes it.
- To retire the monitor after the sprint: `~/.local/bin/hermes cron delete
  mp-factory-monitor` (or leave it: it is silent while `status.json` is
  unchanged).
