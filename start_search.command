#!/bin/bash
# Double-click launcher: starts the search server + a Cloudflare quick tunnel
# if they aren't already running, then prints/copies the public URL.
# Safe to double-click again later — it won't start duplicate processes.

set -e
cd "$(dirname "$0")"

mkdir -p logs

echo "== Dutchie Product Search =="

if pgrep -f "uvicorn app:app" > /dev/null; then
    echo "Server already running."
else
    echo "Starting server..."
    nohup ./.venv/bin/uvicorn app:app --port 8000 > logs/uvicorn.log 2>&1 &
    disown
    sleep 2
fi

if pgrep -f "cloudflared tunnel --url http://localhost:8000" > /dev/null; then
    echo "Tunnel already running."
else
    echo "Starting Cloudflare tunnel..."
    rm -f logs/cloudflared.log
    # Run under a pty (via script(1)): cloudflared fully-buffers its output
    # when writing straight to a redirected file, so the URL line can sit
    # unflushed in memory for a long time. A pty makes it behave as if
    # interactive and flush each line immediately.
    nohup script -q /dev/null cloudflared tunnel --url http://localhost:8000 > logs/cloudflared.log 2>&1 &
    disown
    echo "Waiting for tunnel URL..."
    for i in $(seq 1 20); do
        sleep 1
        if grep -qoE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' logs/cloudflared.log 2>/dev/null; then
            break
        fi
    done
fi

URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' logs/cloudflared.log | head -1)

echo ""
echo "Local:  http://127.0.0.1:8000"
if [ -n "$URL" ]; then
    echo "Public: $URL"
    echo "$URL" | pbcopy
    echo "(public URL copied to clipboard)"
else
    echo "Public URL not found yet — check logs/cloudflared.log"
fi
echo ""
read -p "Press Enter to close this window (the server keeps running in the background)..."
