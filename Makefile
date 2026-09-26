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
        loguru-check secret-check ledger-check \
        frontend-lint frontend-format frontend-test frontend-test-cov frontend-typecheck \
        frontend-collection frontend-e2e \
        gate quality quality-full pre-commit

# Self-developed trees (A1 + A2)
PY_SELFDEV := opendata scripts tests
# Ported tree (B). Milestone A2 renamed akshare/ to opendata_http/; this
# variable was left behind, so `make lint` had been failing on its third
# command (ruff check --select E,F akshare: directory not found) ever since.
PY_PORTED := opendata_http

help:
	@echo "Gate:             gate"
	@echo "A2 (gating):      a2-check public-api-quality zero-dep-check brand-check loguru-check secret-check ledger-check quality-ratchet"
	@echo "Tests:            test test-cov"
	@echo "Dev views (A1):   lint format format-check typecheck security deps-audit"
	@echo "Frontend:         frontend-lint frontend-format frontend-collection frontend-e2e frontend-test frontend-test-cov frontend-typecheck"

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

# B-layer (ported) one-off full scan with manual triage (A2.6). The daily
# `security` target excludes opendata_http by design (quality spec §4: the
# ported tree is byte-faithful to upstream and gets one full review per
# sync, not per-commit debt accounting). Re-run on re-sync:
#   make security-ported
security-ported:
	@# bandit exits 1 when it finds anything; findings are the expected
	@# output here, so tolerate the status and validate the artefact below.
	-bandit -r opendata_http -f json -o docs/evidence/A2/bandit-ported.json
	@python -c "import json;d=json.load(open('docs/evidence/A2/bandit-ported.json'));r=d['results'];from collections import Counter;print('ported scan:', len(r), 'findings ->', dict(Counter(x['test_id'] for x in r)))"

js-points-check:
	python scripts/quality/scan_js_points.py --check

# loguru renders with str.format and logging with %-formatting, and each family
# silently drops the arguments the other one substitutes. A grep cannot tell them
# apart, so this resolves every receiver's binding (module, class attribute,
# injected `self.logger = logger or _default_logger`) before it judges. C38
# counted 12 such calls on the bare-name face; the attribute form it could not
# see held 1 more.
loguru-check:
	python scripts/quality/loguru_render_check.py

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

# Full-history credential scan (AC-16). It used to live only in CI, pinned to its own
# gitleaks release, so five red CI pushes coexisted with five green `make gate` runs
# and the two scanners did not even report the same findings (C36). The pinned device is
# docs/quality/secret-scan.json; a missing or differently-versioned scanner fails the
# gate instead of being echoed away like `deps-audit` does.
secret-check:
	python scripts/quality/secret_scan_check.py

# The acceptance document keeps two books that had never been read against each other:
# 130 item-level criteria in §2-§6 (0 ticked) and 19 AC rows in §10 (eight of them
# saying 完成). A criterion nothing checks cannot fail, so every `proven` item must name
# a command, an ISO date and an evidence file that exists in the repo, and every §10 row
# must disclose 条目级 k/m — claiming 完成 with k<m also requires 未逐条达标.
ledger-check:
	python scripts/quality/acceptance_ledger_check.py

# --- Frontend --------------------------------------------------------------

frontend-lint:
	# Check-only: the gate must never rewrite sources. Use `npm run lint` to auto-fix.
	cd frontend && npx eslint .

frontend-format:
	cd frontend && npm run format

frontend-test:
	cd frontend && npx vitest run --testTimeout=15000

# A collector rule that hides a test file is indistinguishable from a plane
# that never ran, so the run's own green cannot report it. Compare the files on
# disk against the files vitest admits, and fail on the difference.
frontend-collection:
	python scripts/quality/frontend_test_collection.py

frontend-test-cov:
	cd frontend && npm run test:coverage

# `vue-tsc --noEmit` without `-p`/`-b` reads frontend/tsconfig.json, which is a
# solution file (`files: []` + references): it type checks zero files and always
# exits 0, so the item could never fail. `-b` walks the referenced projects and
# `--force` ignores the incremental tsbuildinfo the previous gate run left behind.
frontend-typecheck:
	cd frontend && npx vue-tsc -b --force

# A playwright run reports a declared skip, a test whose only assertion sits
# behind `if (await …isVisible())`, and a test that asserts a substring of the
# path it just visited, all in the same green summary. So the run cannot gate
# itself: the judgment plane reads every leaf first, and only after it is clean
# does the browser run have to be green. Needs `npx playwright install chromium`
# once per machine; it starts only the vite dev server, no backend and no
# warehouse (see docs/evidence/C30/README.md §4 for what that buys and costs).
frontend-e2e:
	python scripts/quality/frontend_e2e_plane.py
	cd frontend && npx playwright test --reporter=list

# --- Aggregates ------------------------------------------------------------

# Every item runs in its own sub-make so a failure aborts the gate immediately
# and the failing item is visible in the log — no summary may mask a sub-failure.
gate:
	@echo "===== gate: brand-check ====="
	@$(MAKE) --no-print-directory brand-check
	@echo "===== gate: zero-dep-check ====="
	@$(MAKE) --no-print-directory zero-dep-check
	@echo "===== gate: secret-check ====="
	@$(MAKE) --no-print-directory secret-check
	@echo "===== gate: ledger-check ====="
	@$(MAKE) --no-print-directory ledger-check
	@echo "===== gate: js-points-check ====="
	@$(MAKE) --no-print-directory js-points-check
	@echo "===== gate: loguru-check ====="
	@$(MAKE) --no-print-directory loguru-check
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
	@echo "===== gate: frontend-collection ====="
	@$(MAKE) --no-print-directory frontend-collection
	@echo "===== gate: frontend-test ====="
	@$(MAKE) --no-print-directory frontend-test
	@echo "===== gate: frontend-e2e ====="
	@$(MAKE) --no-print-directory frontend-e2e
	@echo "===== gate: PASSED ====="

# Backwards-compatible aliases
quality: a2-check js-points-check
quality-full: a2-check frontend-lint frontend-typecheck frontend-collection frontend-test frontend-e2e

pre-commit:
	@command -v pre-commit >/dev/null 2>&1 && pre-commit run --all-files || echo "pre-commit not installed (pip install pre-commit)"
