#!/usr/bin/env bash
# Start the yt-ext helper server.
#
# Port 8188 by default; override with YT_EXT_PORT.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${YT_EXT_PORT:-8188}"

echo "yt-ext helper -> http://127.0.0.1:${PORT}"
echo "hint: leave this running, then right-click a YouTube video and pick \"Summarize video\". Ctrl-C to stop."

export YT_EXT_PORT="$PORT"
exec python3 helper/server.py
