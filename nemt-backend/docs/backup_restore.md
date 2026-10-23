```markdown
# Backup and Restore

Backups are what let a HIPAA audit pass. Restores are what let you survive a bad day.
The two must be tested.

## What is backed up

- **PostgreSQL databases** (`nemt` in prod, `nemt_test` in dev)
- **Uploaded files** — the `storage_dir` directory (or S3 bucket, if using S3)
- **Application secrets** — `FIELD_ENCRYPTION_KEYS`, `JWT_SECRET`, `DATABASE_URL`, etc.

The first two are data. The third is config. Both are needed for a full restore.

## Backup cadence

| Frequency | Retention | Where |
|---|---|---|
| Nightly full backup | 30 days | Off-site (S3, GCS, or a separate physical host) |
| Weekly full backup | 6 months | Off-site |
| Monthly full backup | 6 years | Off-site (this is the HIPAA-required window for audit logs) |

## Nightly backup procedure (PostgreSQL)

Save as `/usr/local/bin/nemt-backup.sh`:

```bash
#!/bin/bash
set -euo pipefail

DATE=$(date +%Y-%m-%d)
BACKUP_DIR="/backups/nemt"
mkdir -p "$BACKUP_DIR"

# Full database dump
PGPASSWORD="$PGPASSWORD" pg_dump \
  --host=localhost \
  --username=nemt_owner \
  --dbname=nemt \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="$BACKUP_DIR/nemt-$DATE.dump"

# Uploaded files
tar czf "$BACKUP_DIR/storage-$DATE.tar.gz" /var/lib/nemt/storage/

# Encrypt the backups with a passphrase stored offline
gpg --symmetric --cipher-algo AES256 \
  --output "$BACKUP_DIR/nemt-$DATE.dump.gpg" \
  --passphrase-file=/etc/nemt/backup.passphrase \
  "$BACKUP_DIR/nemt-$DATE.dump"
rm "$BACKUP_DIR/nemt-$DATE.dump"

# Upload off-site (adjust to your storage backend)
aws s3 cp "$BACKUP_DIR/nemt-$DATE.dump.gpg" s3://nemt-backups/daily/
aws s3 cp "$BACKUP_DIR/storage-$DATE.tar.gz" s3://nemt-backups/daily/

# Clean up local backups older than 7 days
find "$BACKUP_DIR" -type f -mtime +7 -delete
Schedule via cron:

text
0 2 * * * /usr/local/bin/nemt-backup.sh
Restore procedure
Restoring is a stop-the-world operation. All app instances must be stopped first,
or they'll write to the database while you're restoring.

1. Stop the application
bash
# systemd
sudo systemctl stop nemt-backend

# Kubernetes
kubectl scale deployment nemt-backend --replicas=0

# dev (uvicorn in terminal)
# Ctrl+C
2. Restore the database
bash
# download the backup
aws s3 cp s3://nemt-backups/daily/nemt-2026-10-21.dump.gpg .

# decrypt
gpg --decrypt --output nemt.dump \
  --passphrase-file=/etc/nemt/backup.passphrase \
  nemt-2026-10-21.dump.gpg

# drop and recreate (DESTRUCTIVE — verify the target is correct)
psql -U postgres -c "DROP DATABASE nemt;"
psql -U postgres -c "CREATE DATABASE nemt OWNER nemt_owner;"

# restore
pg_restore --host=localhost --username=nemt_owner \
  --dbname=nemt --no-owner --no-privileges nemt.dump

# verify the schema version matches the code
PGPASSWORD=owner_dev_pw psql -U nemt_owner -h localhost -d nemt -c "
  SELECT version_num FROM alembic_version;"
3. Restore files
bash
tar xzf storage-2026-10-21.tar.gz -C /
4. Restore secrets
If FIELD_ENCRYPTION_KEYS was rotated after the backup was taken, the old key must
still be in the list to decrypt the backup's data. This is why the rotation procedure
keeps old keys for 30 days.

5. Start the application
bash
# systemd
sudo systemctl start nemt-backend

# Kubernetes
kubectl scale deployment nemt-backend --replicas=2

# dev
uvicorn app.main:app --port 8000
6. Verify
bash
curl -s http://localhost:8000/health/ready
# → {"status":"ready"}

curl -s -X POST http://localhost:8000/auth/login \
  -H "X-Tenant: platform" \
  -H "Content-Type: application/json" \
  -d '{"email":"root@platform.example.com","password":"..."}'
# → {"access_token":"...","refresh_token":"..."}
Monthly restore drill
Once a month, restore the previous month's backup to a staging database and verify:

alembic_version matches the current code

A sample login works

A sample trip is queryable

A sample claim is queryable

Restore drills catch the failure mode that backups are useless because nobody tested
them. A backup that has never been restored is not a backup.

What if the backup is corrupt?
If pg_restore fails or the data is unreadable:

Try the previous nightly backup (yesterday's).

Try last week's full backup.

If all fail, the backups are not usable — this is why restore drills matter.

Never delete a backup until its successor has been verified.

Retention
Backup type	Keep for	Why
Nightly	30 days	Quick rollback after a mistake
Weekly	6 months	Longer-term recovery
Monthly	6 years	HIPAA requires 6-year retention for audit logs; patient records typically 7 years
