# opendata quality and development commands
#
# Layered per CODE_QUALITY.md:
#   A2 (new / modified self-developed code) — zero tolerance, enforced by a2-check
#   A1 (legacy self-developed code)         — debt ratchet, enforced by quality-ratchet
#   B  (ported akshare code)                — E/F only + one-off security triage
#
# `make gate` is the single fail-closed entry point used by CI and by acceptance.
# `make lint/typecheck/security` are full-tree developer views: they will show A1
# debt by design and are NOT part of the gate. Only the ratchet decides whether
# that debt is acceptable.

.PHONY: help lint format format-check typecheck security deps-audit a2-check \
        test test-cov quality-ratchet public-api-quality zero-dep-check brand-check \
        frontend-lint frontend-format frontend-test frontend-test-cov frontend-typecheck \
        gate quality quality-full pre-commit

# Self-developed trees (A1 + A2)
PY_SELFDEV := opendata scripts tests
# Ported tree (B). Renamed to opendata_http/ in milestone A2.
PY_PORTED := akshare

help:
	@echo "Gate:             gate"
	@echo "A2 (gating):      a2-check public-api-quality zero-dep-check brand-check quality-ratchet"
	@echo "Tests:            test test-cov"
	@echo "Dev views (A1):   lint format format-check typecheck security deps-audit"
	@echo "Frontend:         frontend-lint frontend-format frontend-test frontend-test-cov frontend-typecheck"

# --- A2 zero-tolerance gate ------------------------------------------------

a2-check:
	python scripts/quality/a2_check.py

# --- Developer views (full tree; A1 debt is expected here) ------------------

lint:
	ruff check $(PY_SELFDEV)
	ruff format --check $(PY_SELFDEV)
	ruff check --select E,F $(PY_PORTED)

format:
	ruff check $(PY_SELFDEV) --fix
	ruff format $(PY_SELFDEV)

format-check:
	ruff format --check $(PY_SELFDEV)

typecheck:
	mypy opendata/

security:
	bandit -c bandit.yaml -r opendata scripts

deps-audit:
	@command -v pip-audit >/dev/null 2>&1 && pip-audit || echo "pip-audit not installed (pip install pip-audit)"

# --- Tests -----------------------------------------------------------------

test:
	pytest tests -n 8 -m "not e2e"

test-cov:
	pytest tests -n 8 -m "not e2e" --cov-branch \
		--cov-report=term-missing --cov-report=xml:coverage.xml --cov-report=html:htmlcov \
		--cov-fail-under=84

# --- Ratchet & audits ------------------------------------------------------

quality-ratchet:
	python scripts/quality/ratchet.py

public-api-quality:
	python scripts/quality/public_api.py

zero-dep-check:
	python scripts/codemod/verify_no_akshare.py --self-test
	python scripts/codemod/verify_no_akshare.py

brand-check:
	python scripts/quality/check_brand.py

# --- Frontend --------------------------------------------------------------

frontend-lint:
	# Check-only: the gate must never rewrite sources. Use `npm run lint` to auto-fix.
	cd frontend && npx eslint .

frontend-format:
	cd frontend && npm run format

frontend-test:
	cd frontend && npx vitest run --testTimeout=15000

frontend-test-cov:
	cd frontend && npm run test:coverage

frontend-typecheck:
	cd frontend && npx vue-tsc --noEmit

# --- Aggregates ------------------------------------------------------------

# Every item runs in its own sub-make so a failure aborts the gate immediately
# and the failing item is visible in the log — no summary may mask a sub-failure.
gate:
	@echo "===== gate: brand-check ====="
	@$(MAKE) --no-print-directory brand-check
	@echo "===== gate: zero-dep-check ====="
	@$(MAKE) --no-print-directory zero-dep-check
	@echo "===== gate: a2-check ====="
	@$(MAKE) --no-print-directory a2-check
	@echo "===== gate: quality-ratchet ====="
	@$(MAKE) --no-print-directory quality-ratchet
	@echo "===== gate: public-api-quality ====="
	@$(MAKE) --no-print-directory public-api-quality
	@echo "===== gate: test-cov ====="
	@$(MAKE) --no-print-directory test-cov
	@echo "===== gate: frontend-lint ====="
	@$(MAKE) --no-print-directory frontend-lint
	@echo "===== gate: frontend-typecheck ====="
	@$(MAKE) --no-print-directory frontend-typecheck
	@echo "===== gate: frontend-test ====="
	@$(MAKE) --no-print-directory frontend-test
	@echo "===== gate: PASSED ====="

# Backwards-compatible aliases
quality: a2-check
quality-full: a2-check frontend-lint frontend-typecheck frontend-test

pre-commit:
	@command -v pre-commit >/dev/null 2>&1 && pre-commit run --all-files || echo "pre-commit not installed (pip install pre-commit)"
