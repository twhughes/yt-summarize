#!/usr/bin/env bash
# install.sh — check that this Mac has what yt-summarize needs.
#
# It installs nothing and changes nothing. For each missing piece it prints
# the one command that fixes it. Run it again until every line says "ok".
set -uo pipefail

MISSING=0
ok()   { echo "ok      $1"; }
need() { echo "MISSING $1"; echo "        fix: $2"; MISSING=$((MISSING + 1)); }

[ "$(uname)" = "Darwin" ] && ok "macOS" || need "macOS" "this tool is built for a Mac"

if command -v python3 >/dev/null; then ok "python3"
else need "python3" "xcode-select --install"; fi

if command -v brew >/dev/null; then ok "Homebrew"
else need "Homebrew" "see https://brew.sh (one command, about 5 minutes)"; fi

if command -v yt-dlp >/dev/null; then ok "yt-dlp (downloads the captions)"
else need "yt-dlp (downloads the captions)" "brew install yt-dlp"; fi

CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || { [ -x "$HOME/.local/bin/claude" ] && CLAUDE="$HOME/.local/bin/claude"; }
if [ -n "$CLAUDE" ]; then ok "claude (writes the summary)"
else need "claude (writes the summary)" "curl -fsSL https://claude.ai/install.sh | bash   — then run: claude   and log in (needs a paid Claude plan)"; fi

if command -v whisper-cli >/dev/null && command -v ffmpeg >/dev/null; then
  ok "whisper-cli + ffmpeg (optional: videos with no captions)"
else
  echo "skip    whisper-cli + ffmpeg (optional: videos with no captions)"
fi

echo
if [ "$MISSING" -gt 0 ]; then
  echo "$MISSING thing(s) missing. Run each fix, then run ./install.sh again."
  exit 1
fi

echo "Checking that claude is logged in (one tiny request, up to 60 s) ..."
if REPLY="$(echo 'Reply with the single word: ready' | "$CLAUDE" -p --tools "" --strict-mcp-config 2>&1)"; then
  ok "claude answered: $(echo "$REPLY" | tail -1 | cut -c1-40)"
  echo
  echo "All set. Next: ./run.sh   (then load the extension — see README.md step 3)"
else
  echo "MISSING claude login"
  echo "        fix: run   claude   once, log in, quit with /exit, then run ./install.sh again"
  exit 1
fi
