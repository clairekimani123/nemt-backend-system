```markdown
# HIPAA Compliance Summary

This is a one-page summary of the technical controls in place for HIPAA Security Rule
compliance. Each control is a citation an auditor will ask about, a code location, and
a verification step.

## §164.312(a)(1) — Access Control

**Requirement:** Systems containing ePHI must have technical policies and procedures for
electronic information systems that maintain ePHI to allow access only to those persons
or software programs that have been granted access rights.

**Implementation:**

- Role-based access control (`app/core/permissions.py`). Every endpoint is decorated
  with `require_permission(...)`; only the roles with the specific permission can call it.
- Row-Level Security on every PHI table (`migrations/versions/0001` and later). Even a
  compromised application role cannot see another tenant's data; PostgreSQL enforces it.
- Per-tenant isolation via the `X-Tenant` header and JWT `cid` claim (`app/core/deps.py`).

**Verification:**
```bash
# Log in as company_admin of alpha. Try to fetch a patient in bravo.
curl -H "Authorization: Bearer <alpha-token>" \
  http://localhost:8000/patients/<bravo-patient-id>
# → 404 (RLS returns zero rows; the resource is indistinguishable from missing)
§164.312(a)(2)(i) — Unique User Identification
Requirement: Identify and track the identity of each user accessing ePHI.

Implementation: Every request is authenticated by JWT; the sub claim carries the
user id, and every audit row records actor_user_id. No anonymous access to PHI.

Verification: GET /users/me on any authenticated request returns the current user.
Every row in audit_logs has either actor_user_id or an internal marker in metadata.

§164.312(a)(2)(iv) — Encryption and Decryption (Addressable)
Requirement: Implement a mechanism to encrypt and decrypt ePHI.

Implementation:

In transit: HTTPS in production. The API refuses to start with env=prod and a
public map provider. Reverse-proxy TLS terminates at nginx/Caddy in front of uvicorn.

At rest: Fernet encryption for MFA secrets, Medicaid IDs, and free-text fields
(app/core/security.py). Fernet is AES-128-CBC with HMAC-SHA256 authentication.

Database: full-disk encryption at the infrastructure level.

Verification: test_mfa_secret_is_encrypted_at_rest in tests/test_auth.py reads
the raw DB column and confirms it does not contain the plaintext secret.

§164.312(b) — Audit Controls
Requirement: Record and examine activity in information systems that contain or use ePHI.

Implementation:

Every mutating operation writes an audit_logs row (app/modules/audit/service.py).

Every read of patient-adjacent data writes a phi.read row via
audit_phi_read() (app/core/phi_audit.py).

The audit_logs table is append-only: nemt_app has SELECT, INSERT only. Verified
by test_audit_log_is_append_only.

Six-year retention enforced by purge_expired_audit_logs (nightly Celery task).

Verification:

sql
SELECT actor_user_id, action, created_at
FROM audit_logs
WHERE action = 'phi.read' AND metadata->>'patient_id' = '<patient-uuid>'
  AND created_at >= now() - interval '30 days'
ORDER BY created_at DESC;
§164.312(c)(1) — Integrity
Requirement: Protect ePHI from improper alteration or destruction.

Implementation:

Append-only tables where mutation would be a compliance violation:
audit_logs, trip_events, driver_positions, claim_payments, notification_logs.
Enforced at the DB grant level (no UPDATE, no DELETE for nemt_app).

Soft-delete pattern for user-visible records: archived_at column, never DELETE.
Verified by test_app_role_cannot_hard_delete_phi.

sha256 hash of every uploaded document stored alongside it (Document.sha256).

Verification: tests/test_master_data_isolation.py::test_app_role_cannot_hard_delete_phi.

§164.312(d) — Person or Entity Authentication
Requirement: Verify that a person seeking access to ePHI is the one claimed.

Implementation:

Argon2id password hashing (app/core/security.py). Never plaintext.

TOTP-based multi-factor authentication (app/modules/auth/service.py). Required for
super_admin, optional for others.

Refresh token rotation with family-based reuse detection. A stolen token is detected
the moment it's replayed; the whole family is revoked.

Lockout after max_failed_logins (default 5) for lockout_minutes (default 15).

Rate limiting on /auth/login, /auth/register, /auth/password/forgot,
/auth/mfa/login.

Verification: tests/test_auth.py::test_lockout_after_repeated_failures,
test_refresh_rotation_and_reuse_detection, test_mfa_enrolment_and_login.

§164.312(e)(1) — Transmission Security
Requirement: Guard against unauthorized access to ePHI being transmitted over an
electronic communications network.

Implementation:

TLS in production (reverse-proxy terminator).

Cache-Control: no-store on every response (app/main.py middleware) — proxies and
browsers cannot cache PHI.

X-Content-Type-Options: nosniff, Referrer-Policy: no-referrer,
Content-Security-Policy: sandbox on document downloads.

Data Retention Policy
Data type	Retention	Where enforced
Audit logs	6 years	purge_expired_audit_logs Celery task
Patient records	7 years after last service	Manual decision (Phase 11)
Trip records	7 years	Same as above
Claims	7 years	Same as above
Session tokens (refresh)	14 days, rotated	RefreshToken.expires_at
Password reset tokens	30 minutes, single-use	PasswordResetToken.expires_at
