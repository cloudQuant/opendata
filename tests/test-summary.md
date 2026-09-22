# Test Summary - opendata

## Overview

| Category | Framework | Tests | Status |
|----------|-----------|-------|--------|
| Backend (Python) | pytest + pytest-asyncio | 875 | ✅ All Passing |
| Frontend (Vue/TS) | Vitest + Vue Test Utils | 37 | ✅ All Passing |
| E2E | Playwright (Chromium) | 5 | ✅ All Passing |
| **Total** | | **917** | **✅ All Passing** |

## Backend Test Coverage

- **Overall Coverage**: 68% (3153 statements, 1023 missed)
- **Test Database**: SQLite in-memory (`sqlite+aiosqlite:///:memory:`)
- **Rate Limiting**: Disabled via `TESTING=true` env var

### Coverage by Module

| Module | Coverage | Notes |
|--------|----------|-------|
| `app/models/` | 95-100% | All models well covered |
| `app/core/security.py` | 99% | Auth, JWT, hashing |
| `app/utils/validators.py` | 97% | Input validation |
| `app/utils/helpers.py` | 98% | Formatting helpers |
| `app/api/schemas.py` | 99% | Pydantic schemas |
| `app/services/script_service.py` | 83% | Script CRUD, execution |
| `app/services/interface_loader.py` | 82% | Interface loading |
| `app/services/execution_service.py` | 73% | Execution tracking |
| `app/api/executions.py` | 86% | Execution endpoints |
| `app/core/config.py` | 89% | App configuration |
| `app/api/settings.py` | 77% | Settings endpoints |
| `app/api/rate_limit.py` | 83% | Rate limiting |

### Test Files (Backend)

| File | Tests | Focus |
|------|-------|-------|
| `test_api_auth_full.py` | 12 | Auth register/login/refresh/logout/me |
| `test_api_users_full.py` | 12 | User CRUD, role mgmt, password reset |
| `test_api_tasks_full.py` | 11 | Task CRUD, trigger |
| `test_api_tasks_lifecycle.py` | 10 | Full task lifecycle with scheduler mocking |
| `test_api_interfaces_full.py` | 15 | Interface CRUD, categories |
| `test_api_tables_full.py` | 6 | Table listing, safe name validation |
| `test_api_tables_lifecycle.py` | 11 | Table schema/data/delete/refresh |
| `test_api_data_full.py` | 14 | Download trigger/progress/result |
| `test_api_executions_full.py` | 10 | Execution listing, stats, filters |
| `test_api_settings_full.py` | 5 | Database/warehouse config |
| `test_api_scripts_full.py` | 16 | Script CRUD, scan, toggle, categories |
| `test_validators_full.py` | 24 | Email, schedule, username, password validation |
| `test_helpers_full.py` | 19 | Table name, column clean, format helpers |
| `test_data_acquisition_full.py` | 7 | Data acquisition service |
| `test_script_service_full.py` | 22 | Script service CRUD, execution, scan |
| `test_scheduler_full.py` | 19 | Scheduler trigger config, interval parsing |
| `test_execution_service_full.py` | 9 | Execution service create/update/get |
| `test_akshare_provider.py` | 9 | Provider init, FuncThread, fetch |
| `test_main_app.py` | 7 | Health check, DB utils, rate limit |
| *(existing tests)* | ~637 | Models, security, services, schemas, API |

## Frontend Test Coverage

### Vitest Unit Tests (37 tests)

| File | Tests | Focus |
|------|-------|-------|
| `stores/auth.test.ts` | 14 | Auth store: login, register, logout, persist, restore |
| `utils/request.test.ts` | 2 | Axios instance config, interceptors |
| `router/index.test.ts` | 11 | Route definitions, admin routes, 404 |
| `types/index.test.ts` | 10 | TypeScript type validation |

## E2E Tests (Playwright)

| File | Tests | Focus |
|------|-------|-------|
| `e2e/auth.spec.ts` | 5 | Login/register page loads, redirect, 404 |

## Bugs Fixed During Testing

1. **`app/api/data.py`**: `TaskStatus.SUCCESS` → `TaskStatus.COMPLETED` (enum value didn't exist)
2. **`app/api/data.py`**: `rows_affected` → `rows_after`, `duration_ms` → `duration`, `completed_at` → `end_time` (field name mismatches)
3. **`app/api/data.py`**: `TaskExecution` creation missing required `execution_id` and `script_id` fields
4. **`app/api/data.py`**: Timezone-aware vs naive datetime comparison fix
5. **`tests/test_models.py`**: DataTable tests used non-existent fields (`display_name`, `schema_info`, `interface_id`)
6. **`tests/test_security.py`**: Path traversal test URL normalization issue

## How to Run Tests

### Backend
```bash
# Run all backend tests with coverage
cd /path/to/opendata
python -m pytest --cov=app --cov-report=term-missing

# Run specific test file
python -m pytest tests/test_api_auth_full.py -v
```

### Frontend (Vitest)
```bash
cd frontend
npm test              # Run once
npm run test:watch    # Watch mode
npm run test:coverage # With coverage
```

### E2E (Playwright)
```bash
cd frontend
npx playwright test              # Run all
npx playwright test --ui         # Interactive UI mode
npx playwright show-report       # View HTML report
```
