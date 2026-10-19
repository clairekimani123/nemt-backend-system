
billing-smoke:
	@echo "=== rate engine (pure) ==="
	python -m pytest tests/test_billing.py -k "compute_charge or apply_rules" -q
	@echo ""
	@echo "=== billing API (DB + HTTP) ==="
	python -m pytest tests/test_billing.py -q
	@echo ""
	@echo "=== end-to-end pipeline ==="
	python -m pytest tests/test_billing_e2e.py -q