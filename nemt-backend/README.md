# MediRoutes backend (NEMT, multi-tenant, HIPAA-minded)

FastAPI · async SQLAlchemy 2.0 · PostgreSQL + PostGIS · Redis · Celery · Pydantic v2

> **Synthetic data only** until BAAs are signed with every vendor that touches PHI.

## Quick start
```bash
cp .env.example .env            # then set FIELD_ENCRYPTION_KEYS (command is in the file)
docker compose up -d db redis mailpit
pip install -e ".[dev]"
alembic upgrade head
python -m app.scripts.bootstrap --email you@example.com    # platform company + Super Admin
uvicorn app.main:app --reload                               # http://localhost:8000/docs
celery -A app.workers.celery_app worker -l info             # in another terminal
pytest                                                      # uses the separate nemt_test DB
```
Email sent in dev is caught by Mailpit at http://localhost:8025.

## How tenancy works
Every request runs inside a Postgres transaction tagged with the caller's company
(`set_config('app.current_tenant', ..., true)`). RLS policies compare `company_id` to it and
**fail closed** (no tenant set = zero rows). The app role is not a superuser, not BYPASSRLS,
and doesn't own the tables. Pre-login routes learn the tenant from the `X-Tenant: <slug>` header;
token-bearing routes learn it from the JWT / the `<company_id>.<secret>` token prefix.

## Checklist: adding a tenant-owned table (patients, trips, vehicles...)
1. Model uses `TenantMixin` (`company_id`) + `IdMixin` + `TimestampMixin`.
2. If another tenant table references it, add `UniqueConstraint("company_id", "id")` and use a
   composite FK `(company_id, x_id)` so cross-tenant links are impossible.
3. Migration: call `tenant_policy("table")` and GRANT to `nemt_app` (copy from `0001`).
4. Queries filter `company_id` explicitly in the service layer (RLS is the safety net, not the plan).
5. Protect routes with `require_permission(Permission.X)`; add new permissions in `core/permissions.py`.
6. Write an audit entry for every create/update/**read of PHI** (`audit.record`). IDs only, never PHI in `meta`.
7. Encrypt sensitive columns (`encrypt_field`) and add a test showing the DB value isn't plaintext.
8. Add a cross-tenant test in `tests/`.

## Layout
```
app/core/      config, db (tenant context), security, permissions (RBAC), deps, mail port
app/modules/   auth, users, tenants (platform), audit  -> each: models, schemas, service, router
app/workers/   Celery app + tasks
migrations/    Alembic (RLS + grants are hand-written in 0001)
tests/         tenant isolation, auth, RBAC, audit
```


## Testing & Login Walkthrough

This section walks you from a fresh clone to your first authenticated
Swagger request. It assumes you're on Linux/macOS.

### 1. Prerequisites

- Postgres 15+ running on `localhost:5432`
- Python 3.12+
- Two database roles exist (created once, out of band):

  ```sql
  CREATE ROLE nemt_owner LOGIN PASSWORD 'owner_dev_pw';
  CREATE ROLE nemt_app   LOGIN PASSWORD 'app_dev_pw';
  CREATE DATABASE nemt      OWNER nemt_owner;
  CREATE DATABASE nemt_test OWNER nemt_owner;
  ```

  `nemt_owner` owns the schema (migrations, TRUNCATE).
  `nemt_app` is the runtime role (RLS enforced, least privilege).

### 2. Clone, venv, install

```bash
git clone <repo> nemt-backend-system
cd nemt-backend-system/nemt-backend
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

### 3. Create your `.env`

```bash
cp .env.example .env
# Generate a Fernet key ONCE and paste it into .env's FIELD_ENCRYPTION_KEYS:
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

> **Never regenerate `FIELD_ENCRYPTION_KEYS` after this point.** Any MFA
> secret or PHI field encrypted with the old key becomes undecryptable.
> In production this comes from a secrets manager.

### 4. Build the schema and seed the first super admin

```bash
make migrate            # alembic upgrade head
make bootstrap          # creates the platform company + super admin
```

`make bootstrap` is idempotent — safe to re-run. It uses
`app/scripts/bootstrap.py` and refuses to create a duplicate super admin.

### 5. Start Mailpit (for password-reset emails)

```bash
# Terminal 2
mailpit                 # SMTP :1025, web UI :8025
```

### 6. Start the API

```bash
# Terminal 1
make run                # uvicorn on http://127.0.0.1:8000
```

Open <http://127.0.0.1:8000/docs>.

### 7. Swagger login sequence

The endpoints below are ordered by dependency. Complete each one before
moving to the next.

#### 7.1 Login as super admin

- `POST /auth/login`
- Header: `X-Tenant: platform`
- Body:
  ```json
  {"email": "root@platform.example.com", "password": "Nemt_Transportation"}
  ```
- Copy `access_token`.

#### 7.2 Authorize in Swagger

Click **Authorize** (top right), paste the token, click **Authorize**,
then **Close**. Every subsequent request now carries
`Authorization: Bearer <token>`.

#### 7.3 Create a company

- `POST /platform/companies`
- Body:
  ```json
  {
    "name": "Alpha Transport LLC",
    "slug": "alpha",
    "timezone": "Africa/Nairobi",
    "admin_email": "admin@alpha.example.com",
    "admin_password": "Nemt_Transportation",
    "admin_full_name": "Alpha Admin"
  }
  ```
- `timezone` must be a valid IANA name (`Africa/Nairobi`, `America/New_York`, …), not a date string.

This also creates the first `company_admin` for the new tenant.

#### 7.4 Login as the company admin

- `POST /auth/login`
- Header: `X-Tenant: alpha`
- Body:
  ```json
  {"email": "admin@alpha.example.com", "password": "Nemt_Transportation"}
  ```
- Re-Authorize in Swagger with this token.

#### 7.5 Create staff

- `POST /users` (repeat for each role below)
- Body:
  ```json
  {
    "email": "dispatch@alpha.example.com",
    "password": "Nemt_Transportation",
    "full_name": "Dispatch One",
    "role": "dispatcher"
  }
  ```
- Roles you can create: `dispatcher`, `driver`, `fleet_manager`,
  `billing_staff`, `facility_broker`.
- `role: "super_admin"` is rejected with 403 by design.

#### 7.6 Register a passenger (public endpoint)

- `POST /auth/register`
- Header: `X-Tenant: alpha`  ← this goes in the **Parameters** box, not the JSON body.
- Body:
  ```json
  {
    "email": "sam@example.com",
    "password": "Nemt_Transportation",
    "full_name": "Sam Rider",
    "phone": "+254717793977"
  }
  ```
- `role` is always forced to `passenger`; a client cannot choose it.

#### 7.7 Login as the passenger

- `POST /auth/login`
- Header: `X-Tenant: alpha`
- Body: `{"email": "sam@example.com", "password": "Nemt_Transportation"}`

#### 7.8 (Optional) Enable MFA

- `POST /auth/mfa/setup` → returns `secret` and `otpauth_uri`.
- Add the secret to Google Authenticator.
- `POST /auth/mfa/enable` body `{"code": "<current 6-digit code>"}` → `204`.
- From now on, `POST /auth/login` returns `mfa_required: true` and an
  `mfa_token` (not access/refresh tokens). Complete the flow with
  `POST /auth/mfa/login` body `{"mfa_token": "...", "code": "<code>"}`.

#### 7.9 (Optional) Password reset

- `POST /auth/password/forgot` header `X-Tenant: alpha`, body
  `{"email": "sam@example.com"}` → `202`.
- Open Mailpit (<http://localhost:8025>), copy the token from the
  `reset-password?token=...` link.
- `POST /auth/password/reset` body
  `{"token": "<token>", "new_password": "<new password>"}` → `204`.
- All prior refresh tokens for that user are revoked.

### 8. Verify tenant isolation manually

Two checks that exercise the RLS boundary from the outside:

```bash
# As the runtime role, with no tenant context set, this returns ZERO rows:
PGPASSWORD=app_dev_pw psql -U nemt_app -h localhost -d nemt \
  -c "SELECT count(*) FROM users;"

# As the owner role, it returns all rows (owner bypasses RLS):
PGPASSWORD=owner_dev_pw psql -U nemt_owner -h localhost -d nemt \
  -c "SELECT count(*) FROM users;"
```

Expected: `0` for `nemt_app`, non-zero for `nemt_owner`. If both return
non-zero, RLS is misconfigured — stop and investigate.

### 9. Run the test suite

```bash
make test
```

The suite asserts the same boundaries you just checked manually, plus
MFA, refresh rotation, lockout, audit, and RBAC behaviour.

### 10. Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `ValidationError: Field required` for DB/JWT/Fernet keys | `.env` not found from current directory | `cd` to `nemt-backend/` and re-run |
| `401 Not authenticated` on `/platform/companies` | You didn't log in and Authorize first | Steps 7.1–7.2 |
| `404 Unknown company` on `/auth/login` | `X-Tenant` header missing or wrong slug | Add `X-Tenant: alpha` |
| `ValueError: Fernet key must be 32 url-safe base64-encoded bytes` | `FIELD_ENCRYPTION_KEYS` is malformed | Regenerate and paste into `.env` |
| Password-reset email never arrives | `MAIL_BACKEND=memory` and no Mailpit | Set `MAIL_BACKEND=smtp` and start Mailpit |
| `mfa_required: true` on login, no tokens | MFA is enabled on that user | Complete `POST /auth/mfa/login` |