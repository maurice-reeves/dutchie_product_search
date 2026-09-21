#!/bin/bash
# Double-click helper for the site at https://milehighdispos.com.
#
# The server and the Cloudflare Tunnel are launchd agents now
# (com.mauricereeves.milehighdispos-web / -tunnel), so they start at login and
# restart on their own; this script just makes sure both are loaded, restarts
# the server so a freshly pulled app.py is what's running, and prints status.
# Logs: logs/web.log and logs/tunnel.log.

cd "$(dirname "$0")"
UID_=$(id -u)

echo "== Mile High Dispos =="
for svc in web tunnel; do
    label="com.mauricereeves.milehighdispos-$svc"
    plist="$HOME/Library/LaunchAgents/$label.plist"
    if ! launchctl print "gui/$UID_/$label" >/dev/null 2>&1; then
        echo "Loading $label ..."
        launchctl bootstrap "gui/$UID_" "$plist"
    fi
done
# Restart the server (not the tunnel) so code changes take effect.
launchctl kickstart -k "gui/$UID_/com.mauricereeves.milehighdispos-web"
sleep 3

echo
launchctl list | grep milehighdispos | awk '{printf "%-45s pid %-6s last exit %s\n", $3, $1, $2}'
echo
echo "Local:  http://127.0.0.1:8000"
echo "Public: https://milehighdispos.com"
curl -s -o /dev/null -w "Public check: HTTP %{http_code}\n" https://milehighdispos.com/ || true
