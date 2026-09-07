**Pleiades live health check — September 7, 2026**

Observed approximately 13:55–14:06 UTC. Read-only host/API inspection, plus one seven-token local inference request. No restart, deployment, configuration change, order, or paid inference request. No sealed dataset prices/outcomes or credential contents were inspected.

**Result: both hosts and the existing dashboard are operational; compute capacity is sufficient for a modest Astra paper service. Maia's storage and container memory controls differ materially from the assumed deployment setup.**

| Check | Alcyone | Maia |
|---|---|---|
| Host | `spark-87a1`, NVIDIA GB10 | `maia`, Pi 4 class hardware |
| Uptime | About 2 days 14 hours | About 2 days 14 hours |
| Load average, 1/5/15 minutes | 0.24 / 0.16 / 0.11 | 0.42 / 0.30 / 0.22 |
| RAM | 124,609 MiB total; 96,872 MiB available | 3,795 MiB total; 3,053 MiB available |
| Swap used | Zero | Zero; zram device visible |
| Storage | Root NVMe: 3.3 TB available, 7% used | Root microSD: 99 GB available, 11% used |
| Temperature | GPU 37°C, 0% utilization at sample | 59.9°C, `get_throttled=0x0` |
| Failed systemd units | None | None |
| Containers | `mp-vllm` and two Hermes containers up about 2 days | `mp-sandbox` and `mp-autoheal` healthy, up about 17 hours |
| Restart count | `mp-vllm`: 0; policy `unless-stopped` | `mp-sandbox`: 0 for current container |

Zero restart counters apply to the current containers; they do not establish that containers have never been recreated. The Pi temperature sample does not justify a cooling upgrade by itself.

**Application and job checks**

- Maia `/healthz` returns `status=ok`; `/` returns HTTP 200 and dashboard HTML. All five bot loops report active. Active loops are not evidence that strategies are capital-cleared or profitable.
- At `2026-09-07T14:05:30.956138+00:00`, the feed endpoint returned 100 market rows, with latest timestamp `2026-09-07T14:05:27.188544`: about four seconds old under the runtime's UTC convention. Only timestamps/counts were printed.
- Maia weather reconciliation completed at 13:30:12 UTC and settlement reconciliation at 06:00:06 UTC, both `Result=success`, exit 0. Their timers remain scheduled. Job exit success was checked; underlying settlement labels/reports were not opened.
- Alcyone ladder capture completed successfully at 07:32:08 CDT, exit 0. Factory reconciliation is scheduled, but had no previous execution timestamp in the queried state; its execution is not verified by this check.
- Alcyone's loopback inference health/model endpoints respond. The loaded model is `ykarout/Qwen3.5-9B-NVFP4`, reported context limit 65,536. A tiny completion returned `OK.` using three prompt and four completion tokens. This proves basic inference, not throughput, forecast accuracy or source-paper replication.
- Alcyone's project checkout is still `revival/pleiades-2026-09`, with no tracked modifications reported. Create an isolated checkout/service for Astra; do not switch or reset the active collector checkout.

**Confirmed setup gaps and recommended order**

1. **Move sustained new writes off Maia's microSD.** Both `/srv/money_printer/data` and `/srv/money_printer/logs` resolve directly onto `/dev/mmcblk0p2`; the container's mounts confirm those host paths. `lsblk` shows no attached USB SSD. The prior deployment documentation's SSD assumption is therefore false on this host. Fastest use of existing hardware: put the new collector, ledger and paper runtime on Alcyone's NVMe, with dashboard access through an approved loopback/tunnel arrangement. Alternatively, attach an available USB SSD to Maia and migrate state with a verified backup and controlled cutover. A separate data disk does not require moving the boot filesystem. [Raspberry Pi external-storage documentation](https://www.raspberrypi.com/documentation/configuration/computers/raspberry-pi.html).
2. **Restore container memory accounting before relying on resource limits.** Maia `docker stats` reports `0B / 0B`. Its cgroup controllers are `cpuset cpu io pids`, with no memory controller; Docker reports `MemoryLimit=false` and `SwapLimit=false`. Diagnose the installed OS/kernel boot configuration, then enable the supported memory-controller path during a maintenance window and verify enforcement. Merely adding a Compose memory limit will not establish protection. Set modest service limits after capability is verified. [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/).
3. **Make LAN addressing predictable.** Windows access to `maia.local` timed out resolving; direct-IP HTTP to `192.168.50.41:8050` succeeded. Alcyone resolves/reaches Maia normally. Use DHCP reservations and dependable local DNS/SSH aliases, and check LAN proxy exclusions. The successful SSH path is Windows → `alcyone` → its existing dedicated `id_ed25519_maia` key → `maia.local` as Alcyone's default user `jushoya`. The Windows `melvin` login was rejected. Document the working path rather than distributing more keys.
4. **Keep the two projects' state and deployment independent.** Astra needs its own checkout, container, database and storage root. Preserve the old collector and dashboard on :8050. Alcyone's inference bind to `127.0.0.1:8000` is already appropriate; retain the existing trust boundary. If Maia eventually serves Astra, bind its dashboard to its intended LAN interface rather than blindly copying the current `0.0.0.0:8050` binding.
5. **Monitor useful failure signals.** Add feed/forecast age, reconciliation completion, reserved cash, position exposure, inference cost, disk free space and container restarts to Astra's status. Use bounded log rotation and a checkpointed database backup with a restore drill. Backups and power protection were not inspected here; these are proposed requirements, not claims that the current setup lacks them. A host health probe alone cannot certify executable fills or correct profits.

**Revised delivery estimate**

- Initial live-feed paper simulation and dashboard: **1–2 focused development days**. Operational hosts and working inference remove a bring-up uncertainty; the missing account/execution adapter remains the main work.
- Dependable first deployment: **3–5 days elapsed**, including a **24–48-hour soak** and fault/restart checks.
- If deploying sustained new writes on Maia: budget **2–4 additional hands-on hours** for storage and memory-controller setup, conditional on suitable storage already being available and straightforward OS support. Procurement or unexpected boot/kernel issues are outside this allowance.
- Hosting the initial service on Alcyone's existing NVMe avoids blocking on Maia's SSD. Keep heavy research jobs from competing with the paper runtime. No additional cluster node or GPU is indicated by these samples.

These are engineering estimates, not promises. They assume an available timestamped forecast source and a deliberately bounded initial market universe. The existing local model is a separately evaluated adaptation. Financial validation still takes the prospective period specified in the PRD.

**Reproduction of key read-only checks**

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 alcyone "uptime; free -m; df -h /; systemctl --failed --no-pager"
ssh -o BatchMode=yes -o ConnectTimeout=10 alcyone "curl -fsS --max-time 8 http://maia.local:8050/healthz; curl -fsS --max-time 8 http://127.0.0.1:8000/health"
ssh -o BatchMode=yes -o ConnectTimeout=10 alcyone "ssh -o BatchMode=yes -o ConnectTimeout=8 -i ~/.ssh/id_ed25519_maia maia.local 'uptime; free -m; findmnt -T /srv/money_printer/data; findmnt -T /srv/money_printer/logs; lsblk -o NAME,SIZE,TYPE,MOUNTPOINTS,TRAN; docker stats --no-stream; cat /sys/fs/cgroup/cgroup.controllers; docker info --format={{.MemoryLimit}}; docker info --format={{.SwapLimit}}; vcgencmd get_throttled; vcgencmd measure_temp'"
```

Values are point-in-time observations. No network benchmark, sustained inference load test, backup restore, power-loss test, security audit, or strategy-profitability evaluation was performed.
