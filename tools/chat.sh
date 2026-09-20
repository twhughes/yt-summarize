#!/usr/bin/env bash
# chat.sh — open an interactive Claude session preloaded with a video briefing.
#
# Launched by the helper's POST /chat (via osascript into a new iTerm2 window);
# not meant to be run by hand, though `tools/chat.sh <briefing.md>` works fine.
#
# The briefing is a markdown file (title, URL, summary, full transcript) written
# to ~/.cache/yt-ext/chat/<video_id>.md. We cd into that directory and pass the
# bare filename so Claude's read stays inside its own working directory — no
# repo context, no out-of-cwd permission prompt standing between the user and his
# first question.
set -euo pipefail

BRIEF="${1:-}"
if [ -z "$BRIEF" ] || [ ! -f "$BRIEF" ]; then
  echo "chat.sh: no briefing at '${BRIEF}'" >&2
  echo "(press return to close)" >&2
  read -r _ || true
  exit 1
fi

CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || CLAUDE="$HOME/.local/bin/claude"
if [ ! -x "$CLAUDE" ]; then
  echo "chat.sh: claude CLI not found" >&2
  echo "(press return to close)" >&2
  read -r _ || true
  exit 1
fi

cd "$(dirname "$BRIEF")"
NAME="$(basename "$BRIEF")"

# One submitted turn that loads the file and then gets out of the way, so the
# session is sitting at an empty prompt with the whole transcript in context.
exec "$CLAUDE" "Read $NAME — a YouTube video's summary followed by its full \
auto-generated transcript. This is the only thing we are discussing; there is \
no codebase here and nothing to build. Read it, then reply with exactly one \
line: the video's title followed by ' — loaded, ask away.' Say nothing else \
and do not summarize it. I will ask my question next."
