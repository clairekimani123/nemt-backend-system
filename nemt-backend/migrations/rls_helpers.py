"""Reusable security statements for migrations (so each new table is 2 lines, not 15)."""
from alembic import op


def tenant_policy(table: str, column: str = "company_id") -> None:
    """RLS: rows visible/writable only when `column` equals the request's tenant. Fails closed."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table}
        USING ({column} = (SELECT app_current_tenant()) OR (SELECT app_bypass_rls()))
        WITH CHECK ({column} = (SELECT app_current_tenant()) OR (SELECT app_bypass_rls()))
    """)


def grant(privileges: str, *tables: str, to: str = "nemt_app") -> None:
    op.execute(f"GRANT {privileges} ON {', '.join(tables)} TO {to}")