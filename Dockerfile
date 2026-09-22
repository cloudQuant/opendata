# Multi-stage build for opendata
FROM python:3.11-slim AS builder

# Set working directory
WORKDIR /opendata

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install akshare package
COPY pyproject.toml setup.cfg* ./
COPY akshare/ ./akshare/
RUN pip install --no-cache-dir .

# Copy application code (akshare already cached above)
COPY opendata/ ./opendata/
COPY alembic/ ./alembic/
COPY alembic.ini logging_config.ini ./

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /opendata

# Frontend build stage
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ .
RUN npm run build

# Final stage
FROM python:3.11-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libmariadb3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user in final stage
RUN useradd -m -u 1000 appuser

# Copy Python dependencies from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy akshare package
COPY --from=builder /opendata/akshare /opendata/akshare

# Copy application code
COPY --from=builder --chown=appuser:appuser /opendata /opendata

# Copy frontend build output
COPY --from=frontend-builder --chown=appuser:appuser /frontend/dist /opendata/frontend/dist

# Set working directory
WORKDIR /opendata

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application with gunicorn (multi-worker production mode)
# Override workers via WORKERS env var; defaults to 1 for safety
# For single-process mode: CMD ["uvicorn", "opendata.main:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["sh", "-c", "gunicorn opendata.main:app -w ${WORKERS:-1} -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --timeout 120 --graceful-timeout 30 --access-logfile -"]
