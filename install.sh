#!/usr/bin/env bash
# install.sh — check that this Mac has what yt-summarize needs.
#
# It installs nothing and changes nothing. For each missing piece it prints
# the one command that fixes it. Run it again until every line says "ok".
set -uo pipefail
cd "$(dirname "$0")"

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

if command -v python3 >/dev/null; then
  python3 - <<'CHECK'
from helper import transcribe
model = transcribe.available()
print("ok      local transcription (model found)" if model else
      "skip    optional local transcription: " + transcribe.unavailable_reason())
CHECK
fi
if [ -d /Applications/iTerm.app ] || [ -d "$HOME/Applications/iTerm.app" ]; then
  ok "iTerm2 (optional: Open in terminal)"
else
  echo "skip    iTerm2 (optional: Open in terminal; https://iterm2.com)"
fi

echo
if [ "$MISSING" -gt 0 ]; then
  echo "$MISSING thing(s) missing. Run each fix, then run ./install.sh again."
  exit 1
fi

echo "Checking Claude login and the selected model (uses a small request, 60 s limit) ..."
python3 - <<'CHECK'
import sys
from helper import claude_text, server
try:
    reply = claude_text.run("Reply with the single word: ready", model=server.CLAUDE_MODEL,
                            timeout=60, on_error="raise")
    if not reply:
        raise RuntimeError("Claude returned no text")
except RuntimeError as exc:
    print("FAILED  Claude check: " + str(exc))
    print("        Run claude to check login/quota; check YT_EXT_MODEL and update Claude Code if a flag is unsupported.")
    sys.exit(1)
print("ok      Claude answered using " + server.CLAUDE_MODEL)
print("All set. Start ./run.sh, then open http://localhost:8188/ and paste a video link.")
CHECK
