#!/bin/sh
# Make the voice agent start listening, as if the wake phrase was said.
# Usage: ~/assistant/listen.sh         (or bind it to anything that can run a command)
for port in 80 8080; do
  curl -sf -X POST "http://127.0.0.1:$port/api/voice/listen" -d '{"source":"script"}' && exit 0
done
echo "agent not reachable" >&2
exit 1
