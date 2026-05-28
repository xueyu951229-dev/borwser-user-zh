#!/bin/bash
set -e

# Validate environment configuration
echo "Validating environment configuration..."
python validate_env.py
if [ $? -ne 0 ]; then
    echo "Environment validation failed. Exiting."
    exit 1
fi

./start_all.sh
./novnc_startup.sh

python http_server.py > /tmp/server_logs.txt 2>&1 &

# Install backend dependencies
pip install -r backend/requirements.txt > /tmp/pip_install.log 2>&1

# Start FastAPI server
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload > /tmp/fastapi_stdout.log 2>&1 &

echo "✨ Browser Use Demo with FastAPI is ready!"
echo "➡️  Open http://localhost:8000 in your browser to begin"
echo "🔌 API Health Check: http://localhost:8000/health"

# Keep the container running
tail -f /dev/null
