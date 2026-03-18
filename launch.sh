#!/usr/bin/env bash
# launch.sh — Start the Opika Leads web UI and open a browser
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Kill any existing server on port 5000
lsof -ti:5000 2>/dev/null | xargs kill -9 2>/dev/null || true

# Ensure output dir exists
mkdir -p output

# Start Flask in background
echo "Starting Opika Leads server on http://localhost:5000 ..."
python app.py &
SERVER_PID=$!

# Wait for server to be ready
for i in 1 2 3 4 5; do
    if curl -s http://localhost:5000 > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Open browser
python -m webbrowser "http://localhost:5000" 2>/dev/null || \
    xdg-open "http://localhost:5000" 2>/dev/null || \
    open "http://localhost:5000" 2>/dev/null || true

echo "Server running (PID $SERVER_PID). Press Ctrl+C to stop."
wait $SERVER_PID
