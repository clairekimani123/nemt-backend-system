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


### One thing worth knowing: the test_trip_status_change_is_broadcast test relies on the WebSocket event being sent before the HTTP response returns. That works because the publisher runs before db.commit() in change_status — but if you ever move it after the commit, the ordering could flip. If that test becomes flaky, publish after commit and give the broadcast a tiny asyncio.sleep(0.05) before asserting. For now, the ordering is deterministic enough.

## Real-time

The API exposes a WebSocket at `/ws`. Two things flow over it:

- **Drivers → server:** GPS pings while a trip is active. Each ping is persisted in
  `driver_positions` (append-only), so dispatch has a location history and billing has
  proof of service.
- **Server → dispatchers:** every trip status change (`trip.status`), every assignment
  (`trip.assigned`), and every driver position (`driver.position`) in their tenant.

### Handshake

```js
const ws = new WebSocket("wss://api.example.com/ws");
ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: "<access_token>" }));
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === "auth_ok") { /* connected */ }
  if (msg.type === "auth_err") { /* reconnect with a fresh token */ }
  if (msg.type === "driver.position") { /* update the map */ }
};

## Notifications

Events become messages. `trip.assigned.driver`, `trip.en_route.patient`,
`trip.reminder.patient`, `driver.manifest.daily` — each has a template per
(company, event, channel, language) that the admin UI can edit. A template is a
subject and body with `{{placeholder}}` syntax, no expressions, no filters.

### Engine

`notify(company_id=..., event=..., channel=..., to=..., context={...})` looks up the
template, renders it, hands it to the channel, and writes a `notification_logs` row.
Missing templates write a `skipped` row. Delivery failures write a `failed` row with
the error; they never raise into the caller.

### Channels

- **email** — sends via Celery's `send_email` task (Phase 3). SMTP retries and backoff
  are the task's responsibility, not the engine's.
- **sms** — a placeholder until an SMS provider is chosen. Swap `NullSms` for a real
  class in `app/modules/notifications/channels.py`; the engine never changes.
- **push** — WebPush/VAPID placeholder.

### Batch jobs (Celery beat)

| Task | Schedule | What |
|---|---|---|
| `send_trip_reminders` | every 15 min | 2-hour reminder to patients |
| `send_daily_driver_manifests` | 05:00 UTC | driver's day in one email |

### Admin

- `GET/POST/PATCH /notifications/templates` — read and edit templates
- `GET /notifications/log` — recent sends and skips

Requires `settings:write` (company_admin or super_admin).

### Rules

- Every message is transactional. No marketing copy.
- Never put PHI in an email body. Patient first name, trip ref, and address line are
  fine; date of birth, Medicaid ID, and diagnosis are not. Templates are reviewed
  against this rule before going live.
- The log is append-only. Every attempt is a new row.


## Billing

The billing module turns completed trips into claims, submits them to payers, tracks
payments and denials, and produces the files payers actually accept.

### Claim lifecycle


draft ──submit──▶ submitted ──payment(full)──▶ paid
│
├──payment(partial)──▶ partial
├──denial ─────────▶ denied ──write-off──▶ written_off
└──write-off ───────▶ written_off



Every transition is an audit log entry. `claim_payments` is append-only — payments and
adjustments are rows, never overwrites. To see the history of a claim, query its
payments, not its status column.

### Pricing

Two layers:

1. **Rate plan** (`POST /billing/rate-plans`) — base fare, per-mile, per-minute,
   mobility surcharges, after-hours multiplier, wait time, no-show fee, late-cancel
   fee. One default per company; optional per-broker override.

2. **Rate rules** (`POST /billing/rate-plans/{id}/rules`) — override the base
   computation for specific cases. Each rule can filter on broker, procedure code,
   mobility, and weekday. Two kinds:
   - `flat` — replace the running amount with `amount_cents`
   - `multiplier` — scale the running amount by `multiplier_bp / 10000`

   Rules run in descending priority order. A matching rule with `exclusive: true`
   stops further evaluation.

The pricing function is deliberately simple and deterministic. If the rule engine
returns the wrong answer for a specific contract, add a new rule — do not add code.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/billing/rate-plans` | List rate plans |
| `POST` | `/billing/rate-plans` | Create a plan |
| `PATCH` | `/billing/rate-plans/{id}` | Update a plan |
| `GET` | `/billing/rate-plans/{id}/rules` | List rules for a plan |
| `POST` | `/billing/rate-plans/{id}/rules` | Create a rule |
| `DELETE` | `/billing/rate-plans/{id}/rules/{rule_id}` | Delete a rule |
| `GET` | `/billing/billable-trips` | Completed trips with no claim yet |
| `POST` | `/billing/claims/from-trip/{trip_id}` | Create one claim from a trip |
| `POST` | `/billing/claims/from-trips` | Batch: N trips → N claims |
| `GET` | `/billing/claims` | List claims |
| `GET` | `/billing/claims/{id}` | Get one claim + lines + payments |
| `POST` | `/billing/claims/{id}/submit` | Submit a draft claim |
| `POST` | `/billing/claims/{id}/payments` | Post a payment, adjustment, or denial |
| `POST` | `/billing/claims/{id}/write-off` | Write off a claim |
| `POST` | `/billing/claims/batch-submit` | Submit many claims |
| `POST` | `/billing/claims/batch-pay` | Post the same payment to many |
| `GET` | `/billing/reports/summary?from=&to=` | Revenue, denial rate, DSO, top brokers |
| `GET` | `/billing/claims.csv?from=&to=` | CSV export for a date range |
| `GET` | `/billing/claims/{id}/837p.edi` | X12 5010 837P file |
| `GET` | `/billing/claims/{id}/cms1500.pdf` | Printable CMS-1500 |
| `POST` | `/billing/claims/batch-837p` | One 837P file, N claims |

Permissions: `billing:read` (list, view, export, report) and `billing:write` (create,
submit, pay, write off, edit rate plans). Both held by `super_admin` and
`company_admin`; `billing_staff` has write; `dispatcher` has neither (dispatch can't
see money).

### Exports

**837P** (`application/edi-x12`) — X12 5010 professional claim. Structure is valid and
ready for a clearinghouse. The values (NPI, tax ID, submitter ID, payer ID, member ID)
come from placeholders right now; substituting them from a per-company `billing_settings`
table is Phase 11 work.

**CMS-1500** (`application/pdf`) — a filled-out CMS-1500 form. Printable and mailable.
Not pixel-identical to the official form; an overlay on the official PDF template is
Phase 11 work if a payer requires exact layout.

Both exports are tenant-scoped. A user in company A cannot fetch a claim belonging to
company B, even with a valid claim ID. This is enforced by the same `get_or_404` +
RLS pattern used everywhere else.

### Batch semantics

Batch endpoints (`from-trips`, `batch-submit`, `batch-pay`) share one contract:

- **Per-item outcomes.** Response is `200 OK` with `{requested, succeeded, failed, outcomes[]}`. Each outcome has `index`, `ok`, `id` (if success), `error` (if failure).
- **Each item is atomic, the batch is not.** One failed item rolls back to its own savepoint; the rest proceed. A batch of 100 with one bad claim writes 99 good claims and one error.
- **The commit is at the end.** If the whole request crashes mid-batch, nothing is written. If it completes, all successful items are durable.

### Reports

`GET /billing/reports/summary?from=YYYY-MM-DD&to=YYYY-MM-DD` returns:

```json
{
  "claim_count": 42,
  "billed_cents": 210000,
  "paid_cents": 185000,
  "outstanding_cents": 25000,
  "denied_count": 3,
  "denial_rate_bp": 714,
  "avg_days_to_payment": 18.4,
  "by_payer": [...],
  "top_brokers": [...]
}


billing-smoke:
	@echo "=== rate engine (pure) ==="
	python -m pytest tests/test_billing.py -k "compute_charge or apply_rules" -q
	@echo ""
	@echo "=== billing API (DB + HTTP) ==="
	python -m pytest tests/test_billing.py -q
	@echo ""
	@echo "=== end-to-end pipeline ==="
	python -m pytest tests/test_billing_e2e.py -q