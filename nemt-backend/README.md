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
