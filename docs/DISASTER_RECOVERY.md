# Disaster Recovery

This document covers how FlexyVotes data is backed up, how to restore it, how restores are
tested, and what to do in each failure scenario.

## 1. Objectives

| Tier | Data | RPO (max data loss) | RTO (max downtime) |
|---|---|---|---|
| 1 | PostgreSQL: ballots, voters, payments, audit, results | **≤ 5 min** (RDS PITR) | **≤ 1 h** |
| 1 | Encryption and signing keys | 0 (versioned in Secrets Manager / KMS; offline backup) | ≤ 15 min |
| 2 | Private media: evidence, manifestos | ≤ 24 h (AWS Backup of EFS) | ≤ 4 h |
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

| Layer | Mechanism | Retention |
|---|---|---|
| RDS automated backups + PITR | Enabled on the instance; 5-minute transaction-log shipping | 14–35 days |
| RDS snapshots | Daily automated snapshot, plus a manual snapshot before every release and before opening every election | 90 days; copied to the DR region |
| Logical backup | `deploy/scripts/backup.sh`: `pg_dump` (custom format) → AES-256 (OpenSSL, PBKDF2 with 600k iterations) → `.dump.enc` and `.sha256` → S3 with SSE-KMS | 1 year (S3 lifecycle); local copies 14 days |
| Private media | `backup.sh` with `PRIVATE_MEDIA_DIR`, and/or AWS Backup for EFS | 1 year |
| Keys | `python manage.py keys backup --out keys-YYYYMMDD.enc` (scrypt + AES-GCM), stored offline in two places | Every key change |

S3 bucket settings:
- versioning on;
- Object Lock in compliance mode (30 days);
- cross-region replication;
- deletion only by a break-glass role.

The backup passphrase (`BACKUP_PASSPHRASE`) is kept in Secrets Manager in a **different
account or region** from the backups.

**Schedule.** Example cron on the EC2 host, or an EventBridge-scheduled ECS task:

```cron
# Nightly encrypted logical backup to S3
15 2 * * *  cd /opt/flexyvotes && DATABASE_URL=... BACKUP_PASSPHRASE=... BACKUP_S3_URI=s3://flexyvotes-backups-prod \
            PRIVATE_MEDIA_DIR=/var/lib/docker/volumes/flexyvotes_private_media/_data ./deploy/scripts/backup.sh >> /var/log/fv-backup.log 2>&1
# Monthly restore test (first Sunday)
0 4 1-7 * 0  cd /opt/flexyvotes && BACKUP_PASSPHRASE=... SECRET_KEY=... FIELD_ENCRYPTION_KEYS=... \
            ./deploy/scripts/restore-test.sh "$(ls -t backups/*.dump.enc | head -1)" >> /var/log/fv-restore-test.log 2>&1
```

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

Add a row after every test.

## 5. Restore procedures

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
