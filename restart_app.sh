#!/bin/bash
# opendata restart script
# This script restarts the opendata application

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo -e "${YELLOW}Stopping opendata...${NC}"

# Kill process from PID file if exists
if [ -f "opendata.pid" ]; then
    OLD_PID=$(cat opendata.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo -e "${YELLOW}Killing PID: $OLD_PID${NC}"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 2
        # Force kill if still alive
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f opendata.pid
fi

# Also kill any remaining processes on port 8000
PIDS=$(lsof -ti:8000 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    echo -e "${YELLOW}Killing remaining port 8000 processes: $PIDS${NC}"
    kill -9 $PIDS 2>/dev/null || true
    sleep 2
fi

echo -e "${GREEN}Starting opendata...${NC}"

# Start the application
bash start_app.sh
