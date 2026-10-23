# Field Encryption Key Rotation

The system encrypts MFA secrets and, in future phases, Social Security Numbers,
Medicaid IDs, and other patient identifiers at rest using **Fernet** (AES-128 in CBC
mode with HMAC-SHA256 authentication). Multiple keys can coexist; the *newest* key
encrypts new data, and *all* keys can decrypt. This lets you rotate without ever
having a moment where existing data can't be read.

## When to rotate

- **On schedule**: quarterly (every 90 days) is standard for a system handling PHI.
- **On suspicion**: immediately if a database backup, environment variable, or server
  is compromised.
- **On staff turnover**: rotate when a person with access to the production
  `FIELD_ENCRYPTION_KEYS` leaves the team.

## The key list format

`FIELD_ENCRYPTION_KEYS` is a comma-separated list of Fernet keys. **The first key
encrypts; every key decrypts.**
FIELD_ENCRYPTION_KEYS=NEW_KEY_HERE,OLD_KEY_HERE,OLDER_KEY_HERE

text

Each key is a 44-character URL-safe base64 string. Generating one:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
Example output: Xk7wZ4oB_nF5tJp8yRq2vYc3mH1sL6gE9dA0uKxTzQo=

The rotation procedure
1. Generate the new key
bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
Save the output somewhere temporarily. It's a secret — do not paste it into a chat,
a ticket, or a commit.

2. Prepend the new key to the list
Edit .env (dev) or the secrets manager (production):

bash
# before
FIELD_ENCRYPTION_KEYS=K1
# after
FIELD_ENCRYPTION_KEYS=K2,K1
K2 is the new key. New writes now encrypt with K2. Old data encrypted with K1
still decrypts because K1 is still in the list.

3. Restart the application
The setting is read at startup. New processes pick up the new key list.

Development (uvicorn in a terminal):

bash
# Ctrl+C the running uvicorn process, then restart:
uvicorn app.main:app --reload --port 8000
Production on a single VM (systemd):

bash
sudo systemctl restart nemt-backend
Production on Kubernetes:

bash
kubectl rollout restart deployment/nemt-backend
kubectl rollout status deployment/nemt-backend
Verify:

bash
curl -s http://localhost:8000/health
# → {"status":"ok"}
4. Re-encrypt old data
Old rows are still encrypted with K1. They decrypt fine, so the system is
operational, but you want to move them to K2 before removing K1.

Save as scripts/rotate_keys.py:

python
"""One-shot re-encryption of every mfa_secret_enc with the newest key.

Run once per tenant after prepending the new key. Idempotent: rows already
encrypted with the new key decrypt-and-re-encrypt to the same plaintext.

Usage:
    python -m scripts.rotate_keys
"""
import asyncio

from sqlalchemy import select

from app.core.db import engine, open_tenant_session
from app.core.security import decrypt_field, encrypt_field
from app.modules.tenants.models import Company
from app.modules.users.models import User


async def main() -> None:
    async with open_tenant_session(None, bypass=True) as db:
        companies = (await db.scalars(select(Company).where(
            Company.is_platform.is_(False)))).all()

    total = 0
    for c in companies:
        async with open_tenant_session(c.id) as db:
            users = (await db.scalars(select(User).where(
                User.company_id == c.id,
                User.mfa_secret_enc.is_not(None),
            ))).all()
            for u in users:
                u.mfa_secret_enc = encrypt_field(decrypt_field(u.mfa_secret_enc))
            await db.commit()
            print(f"{c.slug}: re-encrypted {len(users)} rows")
            total += len(users)

    await engine.dispose()
    print(f"done. total rows: {total}")


if __name__ == "__main__":
    asyncio.run(main())
Run it:

bash
python -m scripts.rotate_keys
5. Wait 30 days, then remove the old key
The 30-day wait covers one full backup retention cycle. If you need to restore a
backup from before the rotation, the old key is still available for decryption.

After 30 days:

bash
# before
FIELD_ENCRYPTION_KEYS=K2,K1
# after
FIELD_ENCRYPTION_KEYS=K2
Restart the application (same as step 3). Verify a fresh MFA enrolment works:

bash
curl -s -X POST http://localhost:8000/auth/mfa/setup \
  -H "Authorization: Bearer <token>"
# → returns a new secret, encrypted with K2
What NOT to do
Don't remove the old key before re-encrypting. Any row still encrypted with
K1 becomes permanently unreadable.

Don't rotate and re-encrypt at the same time. Prepend the new key, restart,
re-encrypt, wait, remove. Each step can be verified independently.

Don't rotate without a backup. Before step 1, take a full database backup.

Don't commit the key list to git. .env is git-ignored. In production, use a
secrets manager.

Don't regenerate the key "just to test." Any existing MFA secret becomes
undecryptable and users get InvalidToken errors that look like application bugs.

Quarterly rotation checklist
text
[ ] Full database backup taken and verified (see docs/backup_restore.md)
[ ] New key generated
[ ] New key prepended to FIELD_ENCRYPTION_KEYS in the secrets manager
[ ] All app instances restarted and confirmed healthy
[ ] Re-encryption script run against every company
[ ] Row counts verified (rows encrypted before == rows now encrypted)
[ ] Wait 30 days
[ ] Old key removed from FIELD_ENCRYPTION_KEYS
[ ] App restarted and smoke test passed
[ ] Rotation logged in docs/rotation_log.md with date and key fingerprint (first 8 chars only)
