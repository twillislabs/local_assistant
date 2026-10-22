#!/bin/sh
# Switch voice listening on/off.  Usage: ~/assistant/toggle.sh [on|off]   (no argument = toggle)
case "$1" in
  on) body='{"on":true}' ;;
  off) body='{"on":false}' ;;
  *) body='{}' ;;
esac
for port in 80 8080; do
  curl -sf -X POST "http://127.0.0.1:$port/api/voice/toggle" -d "$body" && echo && exit 0
done
echo "agent not reachable" >&2
exit 1
