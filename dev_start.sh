#!/bin/bash
# Quick development start script

set -e

echo "=== opendata Development Start ==="
echo ""

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $PYTHON_VERSION"

# Check if in project directory
if [ ! -f "pyproject.toml" ]; then
    echo "Error: Not in project root (pyproject.toml not found)"
    exit 1
fi

# Create virtual environment if needed
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q -e ".[dev]"

# Create .env if not exists
if [ ! -f ".env" ]; then
    echo "Creating .env file..."
    cat > .env << 'EOF'
# Application Settings
APP_NAME=opendata
APP_ENV=development
APP_DEBUG=true
APP_VERSION=0.1.0

# Server Settings
HOST=0.0.0.0
PORT=8000
WORKERS=1

# Database Settings (SQLite for quick dev)
DATABASE_URL=sqlite+aiosqlite:///./data/opendata.db

# Authentication Settings
SECRET_KEY=dev-secret-key-change-in-production
ACCESS_TOKEN_EXPIRE_MINUTES=1440
REFRESH_TOKEN_EXPIRE_DAYS=30
ALGORITHM=HS256

# CORS Settings
CORS_ORIGINS=["http://localhost:5173", "http://localhost:3000", "http://localhost:8000"]
CORS_ALLOW_CREDENTIALS=true

# Logging
LOG_LEVEL=INFO

# Data Storage
DATA_DIR=./data
UPLOAD_DIR=./uploads
EOF
fi

# Create data directory
mkdir -p data logs uploads

# Generate secret if needed
if grep -q "your-secret-key-here" .env 2>/dev/null || grep -q "dev-secret-key" .env 2>/dev/null; then
    echo "⚠️  Warning: Using default secret key. Change in production!"
fi

echo ""
echo "=== Ready to start ==="
echo ""
echo "Commands:"
echo "  ./start_app.sh       - Start with uvicorn"
echo "  python -m opendata.cli   - Run CLI commands"
echo "  opendata run         - Start server via CLI"
echo ""
echo "API will be available at:"
echo "  http://localhost:8000"
echo "  http://localhost:8000/docs (API docs)"
echo ""
echo "Default admin credentials:"
echo "  Username: admin"
echo "  Password: admin123"
echo ""
