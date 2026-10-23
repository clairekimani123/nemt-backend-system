
billing-smoke:
	@echo "=== rate engine (pure) ==="
	python -m pytest tests/test_billing.py -k "compute_charge or apply_rules" -q
	@echo ""
	@echo "=== billing API (DB + HTTP) ==="
	python -m pytest tests/test_billing.py -q
	@echo ""
	@echo "=== end-to-end pipeline ==="
	python -m pytest tests/test_billing_e2e.py -q



reports-smoke:
	@echo "=== reporting endpoints (DB + HTTP) ==="
	python -m pytest tests/test_reporting.py -q
	@echo ""
	@echo "=== nightly summary job ==="
	python -m pytest tests/test_reporting_jobs.py -q



security-smoke:
	@echo "=== auth (password, MFA, lockout, refresh) ==="
	python -m pytest tests/test_auth.py -q
	@echo ""
	@echo "=== RBAC + audit trail ==="
	python -m pytest tests/test_rbac_and_audit.py -q
	@echo ""
	@echo "=== tenant isolation (RLS, composite FKs) ==="
	python -m pytest tests/test_tenant_isolation.py tests/test_master_data_isolation.py -q
	@echo ""
	@echo "=== rate limiting ==="
	python -m pytest tests/test_ratelimit.py -q
	@echo ""
	@echo "=== PHI read audit ==="
	python -m pytest tests/test_patients.py -k "audit" -q	
