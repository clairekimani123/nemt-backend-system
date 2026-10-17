"""system reset: app_system_reset() + audit log outlives companies

Revision ID: 0006
Revises: 0005

Retention policy enforced by the reset (see app/modules/tenants/system.py):
  PRESERVED  schema + migrations (alembic_version), PostGIS reference data (spatial_ref_sys),
             the platform company row, and the audit log (append-only compliance record).
  CLEARED    every other table in the public schema: users (all roles, Super Admins included),
             sessions, password-reset tokens, patients + addresses, drivers, vehicles,
             facilities, brokers, documents, trips + events, recurring series, counters,
             route plans/routes/stops, driver positions; and every non-platform company.

The table list is computed at run time (everything EXCEPT the preserved set), so a table added
by a later phase is cleared too instead of leaving orphaned operational rows behind.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The audit log must outlive the companies it describes (like actor_user_id, which never
    # had a FK for the same reason). Without this, a company with history could never be removed.
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT IF EXISTS fk_audit_logs_company_id_companies")

    # SECURITY DEFINER: runs as the table OWNER, because the runtime role is deliberately not
    # granted DELETE/TRUNCATE on PHI tables. Guarded three ways: EXECUTE is granted to the app
    # role only; the caller must be in platform (bypass) context, which the API sets only for an
    # authenticated Super Admin; and the API itself re-checks the password + a typed phrase.
    # One function call = one transaction: it either clears everything or nothing.
    op.execute("""
        CREATE FUNCTION app_system_reset() RETURNS jsonb
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS
        $$
        DECLARE
            preserved text[] := ARRAY['alembic_version', 'spatial_ref_sys', 'audit_logs', 'companies'];
            tbl text;
            n bigint;
            cleared jsonb := '{}'::jsonb;
            targets text;
        BEGIN
            IF NOT app_bypass_rls() THEN
                RAISE EXCEPTION 'system reset requires platform context' USING ERRCODE = 'insufficient_privilege';
            END IF;
            -- one reset at a time; and don't wait forever behind a long-running request
            PERFORM pg_advisory_xact_lock(hashtextextended('app:system-reset', 0));
            SET LOCAL lock_timeout = '15s';

            FOR tbl IN
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public' AND tablename <> ALL (preserved)
                ORDER BY tablename
            LOOP
                EXECUTE format('SELECT count(*) FROM %I', tbl) INTO n;
                cleared := cleared || jsonb_build_object(tbl, n);
            END LOOP;

            SELECT string_agg(format('%I', tablename), ', ' ORDER BY tablename) INTO targets
            FROM pg_tables WHERE schemaname = 'public' AND tablename <> ALL (preserved);

            -- No CASCADE on purpose: if some table outside this list referenced one of these,
            -- the TRUNCATE fails and the whole reset rolls back rather than silently widening.
            IF targets IS NOT NULL THEN
                EXECUTE 'TRUNCATE ' || targets || ' RESTART IDENTITY';
            END IF;

            DELETE FROM companies WHERE NOT is_platform;
            GET DIAGNOSTICS n = ROW_COUNT;
            RETURN cleared || jsonb_build_object('companies', n);
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION app_system_reset() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION app_system_reset() TO nemt_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_system_reset()")
    op.execute("""
        ALTER TABLE audit_logs ADD CONSTRAINT fk_audit_logs_company_id_companies
        FOREIGN KEY (company_id) REFERENCES companies (id) ON DELETE RESTRICT NOT VALID
    """)
