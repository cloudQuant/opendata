# Developer Quick Start Guide

## Prerequisites

- Python 3.11+
- Node.js 20+
- MySQL 8.0+
- Redis (optional, for multi-process metrics)

## Quick Start

### 1. Backend Setup

```bash
# Clone the repository
git clone <repo-url>
cd opendata

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
.\venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Install the opendata package
pip install -e .

# Set up environment
cp .env.example .env
# Edit .env with your database credentials
```

### 2. Database Setup

```bash
# Run migrations
alembic upgrade head

# Create admin user (optional - default exists)
# Default: admin / admin123
# IMPORTANT: Change default password in production!
```

### 3. Start Backend

```bash
# Development mode
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Or use the startup script
./start_app.sh
```

### 4. Frontend Setup

```bash
cd frontend

# Install dependencies
npm install

# Start development server
npm run dev
```

### 5. Access the Application

- Frontend: http://localhost:5173
- API Docs: http://localhost:8000/docs
- Health Check: http://localhost:8000/health

## Project Structure

```
opendata/
├── opendata/                    # Backend (FastAPI)
│   ├── api/                # API routes
│   ├── core/               # Config, database, security
│   ├── data_fetch/         # Data acquisition scripts
│   ├── models/             # SQLAlchemy models
│   ├── services/           # Business logic
│   └── utils/              # Utilities
├── frontend/               # Frontend (Vue 3)
│   ├── src/
│   │   ├── api/            # API clients
│   │   ├── components/     # Vue components
│   │   ├── composables/    # Vue composables
│   │   ├── stores/         # Pinia stores
│   │   └── views/          # Page components
│   └── tests/              # Frontend tests
├── tests/                  # Backend tests
└── alembic/                # Database migrations
```

## Development Workflow

### Backend Development

```bash
# Run linting
make lint

# Format code
make format

# Run tests
make test

# Run tests with coverage (requires 70%)
make test-cov

# Security scan
make security

# Type check
make typecheck

# All quality checks
make quality
```

### Frontend Development

```bash
# Run linting
npm run lint

# Format code
npm run format

# Run tests
npm run test

# Run tests with coverage
npm run test:coverage

# Type check
npx vue-tsc --noEmit
```

### Pre-commit Hooks

```bash
# Install pre-commit hooks
pip install pre-commit
pre-commit install

# Run all checks manually
pre-commit run --all-files
```

## Key Features

### 1. Data Scripts

Located in `opendata/data_fetch/scripts/`, organized by category:
- `stocks/` - Stock market data
- `funds/` - Fund data
- `futures/` - Futures data
- etc.

Each script extends `AkshareProvider` and implements `fetch_data()`.

### 2. Scheduled Tasks

Create tasks via API or UI:
- Cron expressions
- Interval-based
- Daily/weekly/monthly schedules

### 3. Stores (Frontend)

All stores use `useStoreAction` composable for consistent loading/error handling:

```typescript
const listHelper = useStoreList<Task>()
const actionHelper = useStoreAction()

async function fetchTasks() {
  await listHelper.load(
    async ({ page, pageSize }) => {
      const response = await tasksApi.list({ page, page_size: pageSize })
      return { items: response.items, total: response.total }
    },
    { errorMessage: '获取任务列表失败' }
  )
}
```

### 4. Logging

**Backend:**
- Uses Loguru with automatic rotation (10MB, 30 days retention)
- Request ID tracking via `X-Request-ID` header
- JSON format available for production

**Frontend:**
- Use `logger` from `@/utils/logger`
- Replaces `console.error` with structured logging
- Shows error toasts in production

### 5. Error Handling

**Backend:**
- HTTP exceptions with consistent error responses
- Automatic error logging with context

**Frontend:**
- `ErrorBoundary` component catches rendering errors
- Composables handle async errors with loading states

## Testing

### Backend Tests

```bash
# Run all tests
pytest tests/

# Run specific test file
pytest tests/test_auth.py

# Run with coverage
pytest tests/ --cov=app --cov-report=html
```

### Frontend Tests

```bash
# Run unit tests
npm run test

# Run E2E tests
npx playwright test

# Run tests in watch mode
npm run test:watch
```

## Deployment

### Docker (Recommended)

```bash
# Build and start all services
docker-compose up -d

# View logs
docker-compose logs -f backend

# Stop services
docker-compose down
```

### Manual Deployment

1. Set `APP_ENV=production` in `.env`
2. Set strong `SECRET_KEY`
3. Configure database connections
4. Run migrations: `alembic upgrade head`
5. Start with gunicorn: `gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000`

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `APP_ENV` | Environment | `development` |
| `SECRET_KEY` | JWT secret key | (change in production!) |
| `MYSQL_HOST` | Database host | `localhost` |
| `MYSQL_PORT` | Database port | `3306` |
| `MYSQL_USER` | Database user | `root` |
| `MYSQL_PASSWORD` | Database password | (empty) |
| `MYSQL_DATABASE` | Database name | `opendata` |
| `REDIS_URL` | Redis URL (optional) | (none) |
| `LOG_LEVEL` | Logging level | `INFO` |
| `LOG_JSON` | JSON log format | `false` |

## Common Tasks

### Add a New Data Script

1. Create file in `opendata/data_fetch/scripts/<category>/<frequency>/`
2. Extend `AkshareProvider`
3. Implement `fetch_data()` method
4. Script will be auto-discovered and registered

### Add a New API Endpoint

1. Create route in `opendata/api/<module>.py`
2. Add Pydantic schemas in `opendata/api/schemas.py`
3. Register router in `opendata/api/__init__.py`

### Add a New Frontend Page

1. Create view in `frontend/src/views/`
2. Add route in `frontend/src/router/index.ts`
3. Create store if needed in `frontend/src/stores/`
4. Add API client in `frontend/src/api/`

## Troubleshooting

### Database Connection Issues

```bash
# Check database is running
mysql -u root -p -e "SELECT 1"

# Test connection
python -c "from opendata.core.database import engine; print(engine.url)"
```

### Frontend Build Errors

```bash
# Clear node modules and reinstall
rm -rf node_modules package-lock.json
npm install

# Check TypeScript errors
npx vue-tsc --noEmit
```

### Migration Issues

```bash
# Check current migration
alembic current

# Mark migration as complete (use with caution)
alembic stamp head
```

## Getting Help

- Check API docs: http://localhost:8000/docs
- Review existing code patterns in similar files
- Run tests to understand expected behavior
