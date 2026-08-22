# How to restore this archive

Standalone instructions. Assumes no knowledge of the project, no access to the
machine that made the archive, and no tooling beyond `tar`, `gzip`, and
`sha256sum` (all present by default on Linux and macOS; on Windows use Git Bash,
WSL, or 7-Zip).

---

## There is no encryption and no password

**This archive is not encrypted.** It is six ordinary gzip-compressed tar files.
There is no key, no passphrase, no keyfile to find, and no decryption step. If you
are looking for a password, you are looking for something that does not exist —
`tar xzf` is the whole procedure.

This is deliberate. The archive was scanned for credentials before the source
machine was destroyed and contains none: no API keys, no private keys, no webhook
URLs, no tokens. The only match a secret scan produces is the literal placeholder
string `ghp_xxxxxxxxxxxx` in some vendored Hermes documentation. The credentials
that *did* exist on the source VM (`.env`, `.hermes/.env`, `auth.json`, and the
`*.key` files) were excluded from every archive and never copied.

Because there are no secrets in it, encrypting it would add a key that can be lost
without adding protection worth having — and the market tape inside cannot be
regenerated, so a lost key would mean permanent loss. If you later move this
archive somewhere you do not control, encrypt it *at that point*, for that
purpose, and keep the key somewhere separate from the archive.

---

## Restore in three commands

```bash
cd vm_snapshot_2026_08_22

# 1. Verify the archives are intact BEFORE trusting them.
sha256sum -c meta/archive_sha256.txt

# 2. Extract everything.
mkdir -p extracted
for f in archive/*.tgz; do tar xzf "$f" -C extracted; done

# 3. Confirm you got what the manifest promises.
find extracted -type f | wc -l      # expect 6779
```

If step 1 prints anything other than `OK` for all six files, **stop** — the archive
is damaged and extracting it will produce silently corrupt data. Re-copy it from
its source rather than working around the failure.

### Two things that look like failures but are not

This procedure was executed end to end on 2026-08-22 before the source VM was
deleted, so both of these are known and accounted for.

**`tar` exits non-zero on `hermes_config.tgz` on Windows**, printing:

```
tar: .hermes/plugins/money-printer: Cannot create symlink to
     '/home/hoyer/money_printer/hermes_plugin': No such file or directory
tar: Exiting with failure status due to previous errors
```

That is the archive's single symlink, and it pointed at a path on the source VM
that no longer exists anywhere. Every one of the 3,187 real files in that archive
extracts correctly; only the dangling link fails. Nothing is lost — the target it
referenced, `hermes_plugin/`, is tracked in the repository. Linux and macOS create
the dead link without complaint and exit 0. If you want a clean exit on Windows,
extract that one archive with `tar xzf archive/hermes_config.tgz -C extracted
--exclude='.hermes/plugins/*'`.

**The file count is lower than the sum of the archives' contents.** The six
archives list 6,969 entries but yield 6,779 files, because **189 files are stored
in two archives each** — the 54 named run directories are captured whole in
`named_runs.tgz`, and their CSVs and session logs are *also* swept up by
`market_csv.tgz` and `session_logs.tgz`. Extracting all six writes each duplicate
once. The arithmetic closes exactly:

```
6,969 entries - 189 duplicates - 1 symlink = 6,779 files
```

### On Windows without `sha256sum`

```powershell
Get-FileHash -Algorithm SHA256 archive\*.tgz | Format-List Path, Hash
```

Compare the hashes against `meta/archive_sha256.txt` by eye. Git Bash and WSL both
provide the real `sha256sum` and are less error-prone.

---

## What lands where

Paths inside the archives are relative to the source VM's `/home/hoyer`, so after
extraction you get:

| path | contents |
|---|---|
| `extracted/money_printer/logs/_archive/**/*.csv` | The market tape — 12.78 M weather rows, 174 days, 2026-01-27 → 2026-08-22 |
| `extracted/money_printer/logs/` | Final production log (75 MB, 24 days continuous) |
| `extracted/data/` | Calibration, forecast archive, ladders, models, trade journal, exchange state |
| `extracted/data/weather_truth/reconcile/` | 27 daily settlement reconciliation runs |
| `extracted/phase0_evidence/` | Phase 0 acceptance log |
| `extracted/.hermes/` | Hermes agent config, skills, cron definitions |

`extracted/` is git-ignored and fully regenerable — delete and re-extract freely.

---

## Extracting one thing without unpacking 2 GB

```bash
# List what an archive holds
tar tzf archive/data_dir.tgz | less

# Pull a single file out
tar xzf archive/data_dir.tgz -C . data/weather_truth/reconcile/reconcile_2026-08-21.json

# Search the market tape without extracting it at all
tar xzOf archive/market_csv.tgz | grep 'KXHIGHNY-26AUG21'
```

---

## Verifying a restored file is byte-identical to the original

`meta/vm_sha256_spotcheck.txt` holds SHA-256 digests computed **on the source VM**
for five representative files, one per archive. To prove a restore is faithful
rather than merely successful:

```bash
sha256sum extracted/data/weather_truth/reconcile/reconcile_2026-08-21.json
# must equal 18a8bba7310c326576526eb976407b88fb70f3bd38c8b0712ce011271aaf9b90
```

Note that `vm_snapshot_2026_08_22/**` is pinned to `eol=lf` in `.gitattributes`
precisely so these digests survive a checkout on Windows. If a digest fails on a
file you have not edited, check your line endings before suspecting the archive.

---

## What cannot be recovered if this archive is lost

The source VM (`money-printer-preschool-20260322`, project `aerospaceaiagent`,
zone `us-central1-c`) was stopped on 2026-08-22 and its disk deleted thereafter.
Once that disk is gone, **this archive is the only copy.**

The market tape specifically cannot be regenerated at any price: Kalshi prunes
settled markets from its public API after roughly 60 days, so the orderbook history
for every event in this archive is already unfetchable. The same is true of the
27 daily reconcile runs, which were produced by a cron job on a machine that no
longer exists.

Keep at least one copy on separate physical media. The repository does **not**
carry this data — `archive/` is git-ignored, so a `git clone` yields the manifest
and the checksums but not the 197 MB they describe.

**As of the VM's deletion there is exactly one copy of this archive**, on the
workstation that made it. That is a single point of failure for data that cannot
be repurchased at any price. Making the second copy is the highest-value thing
anyone reading this can do.
