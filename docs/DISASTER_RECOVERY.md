# Disaster Recovery

This document covers how FlexyVotes data is backed up, how to restore it, how restores are
tested, and what to do in each failure scenario.

## 1. Objectives

| Tier | Data | RPO (max data loss) | RTO (max downtime) |
|---|---|---|---|
| 1 | PostgreSQL: ballots, voters, payments, audit, results | **≤ 5 min** with RDS PITR; **≤ 24 h** with the compose stack's nightly backups | **≤ 1 h** |
| 1 | Encryption and signing keys | 0 (versioned in Secrets Manager / KMS; offline backup) | ≤ 15 min |
| 2 | Private media: evidence, manifestos | ≤ 24 h (nightly backup, or AWS Backup of EFS) | ≤ 4 h |
| 3 | Redis: cache, rate limits, Celery queue | Not a source of truth | Rebuilt on start |
| 3 | Public media (Cloudinary) | Provider-managed | Re-upload if lost |

Redis is safe to lose:
- Queued notifications are rows in the `Notification` table, and the retry job re-sends them.
- Payment outcomes are recovered by webhook retries and reconciliation.
- Scheduled transitions are recomputed by `lifecycle_tick`.

## 2. What could go wrong, and the response

| Scenario | Response |
|---|---|
| A task or instance dies | ECS replaces it (health check on `/healthz/ready`). Nothing to do. |
| AZ outage | Multi-AZ RDS fails over in about 1–2 min. ECS and ElastiCache run in ≥ 2 AZs. |
| Bad deploy | Roll back to the previous task definition ([DEPLOYMENT.md §6](DEPLOYMENT.md#6-releases-and-rollback)) |
| Data corrupted or deleted by mistake | **PITR** to just before the event, into a new instance (§5.1). Ballots, audit events, payment events and evidence can't be deleted or updated through the app or SQL (triggers), which limits the damage. |
| Database lost or compromised | Restore the latest logical backup from S3 (§5.2), then replay via PITR or reconciliation |
| Region outage | Restore in the DR region from the cross-region snapshot copy or the S3 replica (§5.3) |
| Key loss | Restore from the offline key backup (`keys backup`). **Without the KEK, encrypted columns can't be recovered.** |
| Ransomware or account compromise | S3 Object Lock (compliance mode) keeps backups immutable. Restore into a clean account. |
| Paystack outage during paid voting | Checkouts fail and votes aren't credited. Reconciliation credits late successes when Paystack returns. Tell organizers; optionally extend voting. |
| SMS / email provider outage | OTP sign-ins fail. Switch the election to access-code sign-in, or extend voting (dual approval). Notifications retry automatically. |
| Outage during an open election | After recovery, compare the outage window with the ballots cast. An extension needs dual approval (`EXTEND_VOTING`). Record an incident. |

## 3. Backups

### 3.1 Automatic nightly backups (the `backup` service)

The Docker Compose stack includes a backup worker (`deploy/backup/`), so backups need no
host cron and no manual steps. It starts with everything else (`./deploy.sh` or
`docker compose up -d`).

Every night at `BACKUP_SCHEDULE` (default `0 2 * * *`, in `TIME_ZONE`), the worker does
the following:

1. **Dumps** the database with `pg_dump` (custom format) and archives the private media
   (evidence, manifestos).
2. **Encrypts** both with AES-256 (OpenSSL, PBKDF2 with 600k iterations) using
   `BACKUP_PASSPHRASE`, and writes a `.sha256` checksum file.
3. **Proves the backup is restorable** by decrypting it and reading it back with
   `pg_restore --list`. Only then is it counted as a success.
4. **Keeps** `BACKUP_RETENTION_DAYS` (default 14) of backups on the server, in the
   `backups` volume.
5. **Pushes** every backup not yet uploaded to the private GitHub repository
   `BACKUP_GIT_REPO` (see §3.2):
   - each backup goes in its own folder `backups/flexyvotes-<UTC timestamp>/`;
   - files over `BACKUP_GIT_CHUNK_MB` (45 MB) are split into parts, because GitHub
     rejects files over 100 MB;
   - folders older than `BACKUP_GIT_RETENTION_DAYS` (30) are removed, but the newest 7
     are always kept;
   - with `BACKUP_GIT_KEEP_HISTORY=false` (the default), the branch holds just one
     snapshot commit; set it to `true` to keep every backup in the git history.

Safeguards:

| Safeguard | Why |
|---|---|
| No backups without `BACKUP_PASSPHRASE` (16+ characters) | Nothing unencrypted is ever written or pushed |
| Refuses to push unless GitHub reports the repository as **private** | A public repository can't leak data |
| Refuses code branches (`main`, the default branch, any branch containing `manage.py`) | The nightly force-push can never overwrite the code |
| Refuses to back up an **empty** database | A broken setup can't fill retention with empty backups and push the good ones out |
| Takes a backup when it starts, if the last one is older than 24 h | A new or restarted server is covered right away |
| A missed push is retried the next night | The push syncs every local backup that isn't on GitHub yet |
| Health check: unhealthy if the last backup or push is older than `BACKUP_MAX_AGE_HOURS` (26) | `docker compose ps` shows it, and so does `/console/health/` (**Backups** panel) |

Commands:

```bash
docker compose exec backup fv-backup nightly   # back up and push now
docker compose exec backup fv-backup list      # local backups, and what has been pushed
docker compose exec backup fv-backup health
docker compose logs backup                     # what each run did
```

### 3.2 Setting up the GitHub push (once)

Backups go to the application's own repository (`BACKUP_GIT_REPO`, by default
`https://github.com/deOpty/flexyvotes.git`). They are pushed to a separate **orphan
branch**, `backups`, which shares no history with `main`:

- **Code branches stay clean.** Pushes to `backups` don't trigger CI, which only runs for
  `main` and pull requests, and they can't touch the code.
- **The backups branch is one snapshot commit** of the kept backup sets
  (`BACKUP_GIT_KEEP_HISTORY=false`). It's force-pushed each night, so clones stay small.
  Developers can clone with `git clone --single-branch` to skip it entirely.
- **The worker refuses to push to a code branch.** It rejects `main`, `master`, `develop`
  and the repository's default branch, plus any branch that contains `manage.py`.

1. **Make the repository private.** The worker refuses to push to a public repository:
   go to **Settings → General → Danger Zone → Change visibility → Private**. This is
   required: encrypted or not, voter rolls and ballots must never be downloadable by the
   public.
2. **Create a token.** Go to **Settings → Developer settings → Fine-grained tokens →
   Generate new token**, and set:
   - **Repository access:** *Only select repositories*, then pick `deOpty/flexyvotes`;
   - **Permissions:** *Contents: Read and write*. *Metadata: Read* is added
     automatically.
   - **Expiration:** your choice. Note the date, because when the token expires the
     backup service turns unhealthy until you replace it.
3. **Protect `main`.** The token can write to the whole repository, so add a ruleset under
   **Settings → Rules → Rulesets**: target `main`, *Restrict deletions*, *Block force
   pushes*, *Require a pull request*. A leaked server token then can't change the code.
4. **Add the settings** to `.env` on the server (they're already in `.env.example`):
   ```
   BACKUP_PASSPHRASE='<long random passphrase>'
   BACKUP_GIT_REPO=https://github.com/deOpty/flexyvotes.git
   BACKUP_GIT_TOKEN=github_pat_...
   BACKUP_GIT_BRANCH=backups
   ```
5. **Apply and test.** Run `./deploy.sh`, then
   `docker compose exec backup fv-backup nightly`. The repository should now show a
   `backups` branch.
6. **Store the secrets offline.** Put `BACKUP_PASSPHRASE`, `SECRET_KEY` and the
   encryption keys (`FIELD_ENCRYPTION_KEYS` or `KMS_KEY_ID`, `SIGNING_PRIVATE_KEY`,
   `BLIND_INDEX_KEY`) in a password manager. The backups are useless without them, and
   they are deliberately **not** stored in the repository.

To use a separate private repository instead, set `BACKUP_GIT_REPO` to it; everything
else works the same.

### 3.3 AWS layers (production on RDS)

On AWS, RDS provides its own backup layers in addition to the logical backups:

| Layer | Mechanism | Retention |
|---|---|---|
| RDS automated backups + PITR | Enabled on the instance; 5-minute transaction-log shipping | 14–35 days |
| RDS snapshots | Daily automated snapshot, plus a manual snapshot before every release and before opening every election | 90 days; copied to the DR region |
| Logical backup | `deploy/scripts/backup.sh` (the same encrypted format) → S3 with SSE-KMS (`BACKUP_S3_URI`) | 1 year (S3 lifecycle) |
| Private media | AWS Backup for EFS | 1 year |
| Keys | `python manage.py keys backup --out keys-YYYYMMDD.enc` (scrypt + AES-GCM), stored offline in two places | Every key change |

S3 bucket settings:
- versioning on;
- Object Lock in compliance mode (30 days);
- cross-region replication;
- deletion only by a break-glass role.

Keep `BACKUP_PASSPHRASE` in Secrets Manager, in a **different account or region** from
the backups.

## 4. Restore testing

`deploy/scripts/restore-test.sh <backup.dump.enc>` proves that a backup is usable. It:

1. starts a throw-away `postgres:16-alpine` container;
2. verifies the checksum, decrypts and restores the backup;
3. runs the app image against it with the production keys:
   - `migrate --check` (the schema matches the code);
   - `verify_integrity --skip-evidence`: audit hash chains, signed snapshots and
     certifications verify; every data key unwraps and encrypted columns decrypt with the
     production keys. A restore with the wrong keys fails here;
   - row counts for events, voters, ballots, payments and audit events;
4. prints the restore duration, to compare with the RTO;
5. destroys the container.

Run it monthly, after every schema change, and after changing keys. It needs a Linux host,
because it uses `--network host`; run it on the EC2 host or in CI.

### Restore test log

| Date | Backup | Result |
|---|---|---|
| 2026-10-05 | Local development database, through `backup.sh` | **Passed.** The checksum verified, and the restore matched the live database on every table checked: events 3/3, voters 21/21, audit events 19/19, migrations 65/65. All 8 append-only triggers were restored, and `migrate --check` was clean. `verify_integrity` passed on all three audit chains. A tampered backup was rejected by checksum, and a wrong passphrase was rejected. While testing, a GNU-only option in `restore.sh` was replaced so it also works on BusyBox (SECURITY.md B13). |

| 2026-10-23 | Compose `backup` / `restore` services, with a local git repository standing in for GitHub | **Passed.**<br>**Fresh install:** restore-on-setup found an empty database and an empty repository, and started fresh. The first backup was taken, verified and pushed, and all services were healthy.<br>**Nightly run:** with real encrypted data and a 2 MB evidence file, the media archive was split into 3 parts (chunk size set to 1 MB).<br>**New server** (empty volumes): the newest backup was fetched from the repository and its parts rejoined. The voter's encrypted name and email decrypted, the evidence file's SHA-256 matched, restored files were writable by the app user, and `verify_integrity` passed.<br>**Corrupt newest backup:** skipped automatically, and the previous one restored.<br>**Restarting an initialised database:** no restore.<br>**Refusals:** an empty database, a push without a token, and a token that can't prove the repository is private.<br>**Code-repository mode:** with a stand-in repository that has a `main` branch containing `manage.py`, backups went to an orphan `backups` branch with no shared history, one snapshot commit, and `main` untouched. `BACKUP_GIT_BRANCH=main` was refused, as was a branch containing code. A new server restored from the `backups` branch.<br>**Real repository:** pointing at `deOpty/flexyvotes` was refused because it is public. |

Add a row after every test.

## 5. Restore procedures

### 5.0 Automatic restore on setup (Docker Compose stack)

The one-shot `restore` service runs on every `docker compose up`, before web, worker and
beat start:

- **Database already initialised:** it does nothing.
- **Database empty** (a new server, or a lost `postgres_data` volume):
  1. It picks the newest local backup. If there are none, it clones the newest sets from
     `BACKUP_GIT_REPO`.
  2. It verifies the checksum, then restores the database and the private media.
  3. Then the app starts and applies any newer migrations.
- **Newest backup won't restore:** it tries the next older one, up to three.
- **Every candidate fails, or the repository can't be reached:** start-up **stops**,
  rather than continuing with an empty database. Set `BACKUP_RESTORE_ON_SETUP=false` to
  start empty on purpose.
- **Restoring a specific backup:** set `BACKUP_RESTORE_FILE=/backups/<name>.dump.enc`.

To move FlexyVotes to a new server:

1. Install Docker and clone the repository.
2. Copy the **old** server's `.env`. At minimum this means `SECRET_KEY`, the encryption
   keys, `BACKUP_PASSPHRASE`, `BACKUP_GIT_REPO` and `BACKUP_GIT_TOKEN`.
3. Run `./deploy.sh`.

The newest backup is restored from GitHub automatically. Data written after that backup
(up to one day) is lost; to keep it, take a backup on the old server first if it is still
running: `docker compose exec backup fv-backup nightly`.

To recover the **current** server from a backup (for example, after data was deleted by
mistake):

```bash
docker compose stop web worker beat backup
docker compose exec db psql -U flexyvotes -d flexyvotes -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
docker compose run --rm restore        # restores the newest backup into the now-empty database
docker compose up -d
```

To restore an older backup instead, add
`-e BACKUP_RESTORE_FILE=/backups/flexyvotes-<stamp>.dump.enc` to the `run` command. Take a
fresh backup first (`fv-backup backup`) so the current state isn't lost.

### 5.1 Point-in-time recovery (RDS)

1. **Find the target time.** Use the audit log, `/api/v1/audit` or CloudWatch to find the
   last good moment, in UTC.
2. **Restore into a new instance:**
   ```bash
   aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier flexyvotes-prod \
     --target-db-instance-identifier flexyvotes-pitr-$(date +%Y%m%d%H%M) --restore-time 2026-10-05T09:41:00Z \
     --db-subnet-group-name <private-subnets> --vpc-security-group-ids <sg-rds> --multi-az
   ```
3. **Check it** with a one-off task pointed at the new instance:
   ```bash
   DATABASE_URL=postgres://flexyvotes_owner:…@<pitr-endpoint>:5432/flexyvotes python manage.py verify_integrity
   ```
4. **Cut over.**
   - Scale web, worker and beat to 0.
   - Update the `DATABASE_URL` secrets to point at the new instance.
   - Scale back up.
5. **Catch up and record.**
   - Run reconciliation for the gap: `/console/payments/reconciliation/`, with a window
     covering the restore point until now. This recovers payments that succeeded after the
     restore point.
   - Record an incident with the restore point and the reason.

### 5.2 From a logical backup

```bash
aws s3 cp s3://flexyvotes-backups-prod/<stamp>/ ./restore/ --recursive
createdb -h <new-host> -U flexyvotes_owner flexyvotes              # must be an EMPTY database
TARGET_DATABASE_URL=postgres://flexyvotes_owner:…@<new-host>:5432/flexyvotes \
BACKUP_PASSPHRASE=… ./deploy/scripts/restore.sh restore/flexyvotes-<stamp>.dump.enc
DATABASE_URL=… python manage.py migrate --check && python manage.py verify_integrity
```

Private media: decrypt `*.media.tgz.enc` with the same `openssl enc -d` parameters, and
extract it into the private-media volume.

### 5.3 Region failover

1. Restore the latest cross-region snapshot (or the S3 logical backup) in the DR region.
2. Deploy the same image tag with the DR-region copies of the secrets. Use a multi-region
   KMS key, or the replicated `FIELD_ENCRYPTION_KEYS` secret.
3. Recreate ElastiCache, which starts empty.
4. Point DNS at the DR ALB. A Route 53 health-checked failover record makes this automatic.
5. Update the Paystack and Africa's Talking callback allow-lists with the DR NAT IPs.
6. Run reconciliation for the window since the snapshot.

## 6. After any restore

- [ ] `python manage.py migrate --check` is clean.
- [ ] `python manage.py verify_integrity` passes. A break shows exactly where data is
  missing or changed.
- [ ] `python manage.py keys status` reports no errors (data keys unwrap).
- [ ] Published elections still verify: `python tools/verify_election.py` against
  `/verify/<id>/bundle.json`.
- [ ] Reconciliation has run for the gap; discrepancies are resolved.
- [ ] Ballot counts are compared with the last known `ElectionResult` and the turnout
  figures.
- [ ] An incident is recorded, with the timeline and the restore point.
