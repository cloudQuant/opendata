#!/bin/bash
# opendata startup script
# This script starts the opendata application with all required services

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed${NC}"
    exit 1
fi

# Step 1: Install the opendata package
echo -e "${GREEN}Step 1: Installing the opendata package...${NC}"
pip install -e . --upgrade

# Step 2: Install web dependencies
echo -e "${GREEN}Step 2: Installing web dependencies...${NC}"
pip install fastapi uvicorn[standard] pydantic pydantic-settings \
    sqlalchemy alembic pymysql cryptography python-jose passlib \
    python-multipart apscheduler croniter httpx python-dotenv loguru \
    email-validator slowapi sqlalchemy-utils aiomysql aiosqlite click \
    pandas numpy requests beautifulsoup4 lxml openpyxl html5lib \
    jsonpath tqdm tabulate scipy decorator mini-racer --upgrade

# Check if .env exists
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}Creating .env from .env.example...${NC}"
    cp .env.example .env
    echo -e "${YELLOW}Please edit .env file with your configuration${NC}"
fi

# Create necessary directories
mkdir -p logs data uploads

# Run database migrations if alembic is available
echo -e "${GREEN}Step 3: Running database migrations...${NC}"
alembic upgrade head || echo -e "${YELLOW}Migration skipped or failed${NC}"

# Start the application
echo -e "${GREEN}Starting opendata...${NC}"

# Run uvicorn in background
nohup uvicorn opendata.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --log-config logging_config.ini \
    >> logs/uvicorn.log 2>&1 &

APP_PID=$!
echo "$APP_PID" > opendata.pid

# Wait briefly and check if process is still running
sleep 3
if kill -0 "$APP_PID" 2>/dev/null; then
    echo -e "${GREEN}opendata started successfully (PID: $APP_PID)${NC}"
    echo -e "${GREEN}Application: http://localhost:8000${NC}"
    echo -e "${GREEN}API docs:    http://localhost:8000/docs${NC}"
    echo -e "${GREEN}Log file:    logs/uvicorn.log${NC}"
else
    echo -e "${RED}Failed to start opendata. Check logs/uvicorn.log for details${NC}"
    exit 1
fi
