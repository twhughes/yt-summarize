#!/usr/bin/env python3
"""yt-ext helper server.

A tiny localhost bridge between the YT Summarize browser extension and the
local summarize pipeline (yt-dlp auto-captions -> claude CLI).

Python 3 standard library only. No pip dependencies.

    GET    /health              -> {"ok": true}
    POST   /summarize           -> {"url": "https://www.youtube.com/watch?v=..."}
    POST   /chat                -> same body; briefing + interactive Claude in iTerm2
    POST   /ask                 -> {"id"|"url", "question", "history"} -> {"reply"}
    GET    /                    -> the bucket page (helper/index.html)
    GET    /recent              -> [{video_id, url, meta, summary, generated_at}]
    GET    /transcript?v=<id>   -> the cleaned transcript as text/plain
    GET    /config              -> {"prompt", "default_prompt", "is_default"}
    POST   /config              -> {"prompt": "..."} ("" or default text = reset)
    DELETE /summary/<video_id>  -> drop one cached summary (+ its transcript/briefing)

Port defaults to 8188; override with
the YT_EXT_PORT environment variable.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

# The yt-dlp/VTT engine lives beside this file. helper/ is on sys.path when the
# server is run as a script (`python3 helper/server.py`, how run.sh starts it);
# the relative form covers being imported as part of a package.
try:
    from captions import (COOKIES_FROM_BROWSER, YT_DLP, clean_vtt, fetch_captions,
                          is_no_captions, read_duration, read_meta)
    import claude_text as llm
    import transcribe
except ImportError:                                   # pragma: no cover
    from .captions import (COOKIES_FROM_BROWSER, YT_DLP, clean_vtt, fetch_captions,
                           is_no_captions, read_duration, read_meta)
    from . import claude_text as llm
    from . import transcribe

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_PORT = 8188
PORT = int(os.environ.get("YT_EXT_PORT", DEFAULT_PORT))
HOST = "127.0.0.1"

# Everything yt remembers lives here. YT_EXT_CACHE points it somewhere else —
# an empty directory gives a clean archive (a demo, a test run).
CACHE_DIR = Path(os.environ.get("YT_EXT_CACHE") or Path.home() / ".cache" / "yt-ext")

# User-editable settings (currently just the summary prompt). Read fresh on
# every use, so edits apply to the next summary without a restart.
CONFIG_PATH = CACHE_DIR / "config.json"

# Fetched transcripts, cached so a chat doesn't re-run yt-dlp on every turn.
# A subdirectory, invisible to cache_list()'s non-recursive glob — transcripts
# never show up as archive rows.
TRANSCRIPT_DIR = CACHE_DIR / "transcripts"

# "Chat about this video" briefings. A subdirectory, which cache_list()'s
# non-recursive *.json glob never sees, so briefings can never turn up in the
# archive. The terminal Claude runs with this as its working directory, which
# keeps its file reads inside a scratch dir instead of the repo.
CHAT_DIR = CACHE_DIR / "chat"
ITERM_TIMEOUT = 20         # seconds — just the AppleScript launch

# The bucket page lives next to this file and is read from disk on every
# request, so editing it and reloading the tab is enough — no restart.
HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"

# URL parsing and summary rendering are shared with the extension, which has
# to keep its own copy inside the bundle. Rather than duplicate the file, the
# bucket page loads the extension's copy from here. One source of truth.
SHARED_JS = HERE.parent / "extension" / "shared.js"

RECENT_LIMIT = 200

# YT_DLP / YT_DLP_TIMEOUT now live in captions.py (imported above).
CLAUDE_MODEL = os.environ.get("YT_EXT_MODEL") or "claude-sonnet-5"

CLAUDE_TIMEOUT = 240       # seconds

# Transcript length guardrails (characters).
MAX_TRANSCRIPT = 150_000
HEAD_KEEP = 100_000
TAIL_KEEP = 30_000

# The factory prompt. "Reset to default" on the bucket page restores exactly
# this text; a custom prompt lives in config.json and never touches this file.
DEFAULT_PROMPT_INSTRUCTIONS = (
    "Summarize this YouTube video. Be tight and structured. Output exactly, "
    "and nothing else:\n"
    "1. First line: 'TITLE | CHANNEL | DURATION'.\n"
    "2. A blank line, then a TL;DR of 1-2 sentences stating what the video "
    "argues or shows -- the claim itself, not a description of the video.\n"
    "3. A blank line, then 3-5 bullets, each starting with '- '. Each bullet "
    "is EXACTLY one sentence and opens with a 2-4 word plain-text label "
    "followed by a colon, e.g. '- Core claim: ...', '- The evidence: ...', "
    "'- What to do: ...'.\n"
    "4. Optionally one final line 'Bottom line: ...' if the video has a clear "
    "takeaway; omit it otherwise.\n"
    "Hard limits: at most 120 words total after the first line -- keep the "
    "TL;DR under 35 words and every bullet under 22 words, cutting hedges, "
    "examples and qualifiers to stay inside them. Never use "
    "asterisks or any other markdown emphasis anywhere -- the labels are plain "
    "text. Ignore sponsor reads, merch/Patreon plugs, like-and-subscribe and "
    "self-promo segments entirely; do not mention them. No preamble, no "
    "closing remarks."
)

# Chat-about-a-video answers ( POST /ask ). Kept short and grounded.
ASK_INSTRUCTIONS = (
    "You are answering questions about one YouTube video. Below are its "
    "metadata, a summary, the full transcript (auto-generated captions: no "
    "speaker labels, proper nouns often mangled — read through the errors), "
    "and the conversation so far. Answer the final question directly, "
    "grounded in what the transcript actually says; quote or closely "
    "paraphrase where useful. If the video does not cover it, say so plainly "
    "instead of guessing. Plain text only — no markdown emphasis, no "
    "headings. Be tight: usually under 150 words unless the question "
    "explicitly asks for depth."
)

ASK_HISTORY_TURNS = 12         # last N turns sent back to the model
ASK_MAX_QUESTION = 4000        # characters


# --------------------------------------------------------------------------
# Config (the user-editable prompt)
# --------------------------------------------------------------------------

def config_read():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def config_write(cfg):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2)
        return True
    except OSError as exc:
        log("config write failed: %s" % exc)
        return False


def current_prompt():
    """The prompt in force: the config override if set, else the default."""
    prompt = config_read().get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return DEFAULT_PROMPT_INSTRUCTIONS


def set_prompt(prompt):
    """Store a custom prompt; empty/None (or the default itself) resets."""
    cfg = config_read()
    if (isinstance(prompt, str) and prompt.strip()
            and prompt.strip() != DEFAULT_PROMPT_INSTRUCTIONS.strip()):
        cfg["prompt"] = prompt
    else:
        cfg.pop("prompt", None)
    return config_write(cfg)


def config_payload():
    return {
        "prompt": current_prompt(),
        "default_prompt": DEFAULT_PROMPT_INSTRUCTIONS,
        "is_default": current_prompt() == DEFAULT_PROMPT_INSTRUCTIONS,
    }


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

VIDEO_ID = r"[A-Za-z0-9_-]{11}"

URL_PATTERNS = [
    # https://www.youtube.com/watch?v=VIDEOID
    re.compile(
        r"^https?://(?:www\.|m\.|music\.)?youtube\.com/watch\?(?:[^&]*&)*v=(" + VIDEO_ID + r")(?:[&#].*)?$"
    ),
    # https://youtu.be/VIDEOID
    re.compile(r"^https?://youtu\.be/(" + VIDEO_ID + r")(?:[?#].*)?$"),
    # https://www.youtube.com/shorts/VIDEOID
    re.compile(
        r"^https?://(?:www\.|m\.)?youtube\.com/shorts/(" + VIDEO_ID + r")(?:[?#/].*)?$"
    ),
    # https://www.youtube.com/live/VIDEOID  (same shape, harmless to accept)
    re.compile(
        r"^https?://(?:www\.|m\.)?youtube\.com/live/(" + VIDEO_ID + r")(?:[?#/].*)?$"
    ),
]


def extract_video_id(url):
    """Return the 11-char video id for a supported YouTube URL, else None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    for pattern in URL_PATTERNS:
        match = pattern.match(url)
        if match:
            return match.group(1)
    return None


def canonical_url(video_id):
    return "https://www.youtube.com/watch?v=" + video_id


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def cache_path(video_id):
    return CACHE_DIR / (video_id + ".json")


def cache_read(video_id):
    path = cache_path(video_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def cache_write(video_id, payload):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_path(video_id), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except OSError as exc:
        log("cache write failed for %s: %s" % (video_id, exc))


def cache_delete(video_id):
    """Remove one cached summary. True if a file was actually removed.

    The transcript and chat briefing ride along — deleting a video from the
    archive should leave nothing of it behind.
    """
    for stray in (TRANSCRIPT_DIR / (video_id + ".json"),
                  CHAT_DIR / (video_id + ".md")):
        try:
            stray.unlink()
        except OSError:
            pass

    path = cache_path(video_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log("cache delete failed for %s: %s" % (video_id, exc))
        return False


def cache_list(limit=RECENT_LIMIT):
    """Every readable cached summary, newest first by generated_at.

    Junk in the cache directory is skipped, never fatal — this endpoint has to
    survive a half-written file or somebody's stray notes.
    """
    entries = []
    try:
        paths = sorted(CACHE_DIR.glob("*.json"))
    except OSError:
        return []

    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                item = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        video_id = item.get("video_id") or path.stem
        summary = item.get("summary")
        if not isinstance(video_id, str) or not isinstance(summary, str):
            continue
        entries.append({
            "video_id": video_id,
            "url": item.get("url") or canonical_url(video_id),
            "meta": item.get("meta") or "",
            "summary": summary,
            "source": item.get("source") or "captions",
            "generated_at": item.get("generated_at") or "",
        })

    entries.sort(key=lambda item: item["generated_at"], reverse=True)
    return entries[:limit]


# --------------------------------------------------------------------------
# Transcript length
# --------------------------------------------------------------------------

def truncate_middle(transcript):
    """Keep the head and tail of an overlong transcript."""
    if len(transcript) <= MAX_TRANSCRIPT:
        return transcript
    head = transcript[:HEAD_KEEP]
    tail = transcript[-TAIL_KEEP:]
    return head + "\n\n[... transcript truncated ...]\n\n" + tail


# --------------------------------------------------------------------------
# Transcript fetch + cache
# --------------------------------------------------------------------------

def transcript_path(video_id):
    return TRANSCRIPT_DIR / (video_id + ".json")


# --------------------------------------------------------------------------
# Progress — what the one running job is doing right now, for the UIs' bar
# --------------------------------------------------------------------------
# video_id -> {"stage", "pct", "note", "source"}. Only ever one running job
# (both UIs pump strictly one at a time), but keyed by id so a stale poll for
# another video reads "idle" rather than someone else's bar.

PROGRESS = {}
PROGRESS_LOCK = threading.Lock()


def progress_set(video_id, stage, pct=None, note="", source="captions"):
    with PROGRESS_LOCK:
        PROGRESS[video_id] = {
            "stage": stage, "pct": pct, "note": note, "source": source,
        }


def progress_get(video_id):
    with PROGRESS_LOCK:
        return dict(PROGRESS.get(video_id) or {"stage": None})


def progress_clear(video_id):
    with PROGRESS_LOCK:
        PROGRESS.pop(video_id, None)


NO_CAPTIONS_DETAIL = (
    "YouTube has no caption track for this video — the uploader turned "
    "captions off, or auto-captions were never generated. Nothing to "
    "summarize. (Checked every yt-dlp player client; this is not the "
    "rate limit.)"
)


def no_captions_payload(meta):
    """The 422 body for a video YouTube serves no captions for."""
    detail = NO_CAPTIONS_DETAIL
    if meta:
        detail = "%s\n\n%s" % (meta, detail)
    return {"error": "no captions available", "detail": detail}


def fetch_transcript(video_id):
    """The transcript (already cleaned + truncated) and meta for one video.

    Cached under transcripts/<id>.json so chat turns and copy-to-clipboard
    never re-run yt-dlp. Returns (transcript, meta, err_status, err_payload) —
    transcript is None exactly when the err pair is set.
    """
    path = transcript_path(video_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        transcript = stored.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            return transcript, stored.get("meta") or "", None, None
    except (OSError, ValueError):
        pass

    source = "captions"

    url = canonical_url(video_id)
    with tempfile.TemporaryDirectory(prefix="yt-ext-") as workdir:
        cookies = config_read().get("cookies_from_browser") or COOKIES_FROM_BROWSER
        progress_set(video_id, "captions", None, "fetching captions")
        vtt_path, meta, stderr = fetch_captions(workdir, url, cookies or None)

        if vtt_path is None:
            if is_no_captions(stderr):
                # yt-dlp positively said "no caption track" on every attempt:
                # the ONE case the local whisper fallback covers.
                transcript, source, err = transcribe_fallback(video_id, workdir, url, cookies)
                if transcript is None:
                    return None, meta, err[0], err[1]
            elif stderr.strip():
                # A 429 / timeout / anything with a story is transient: report
                # it, never fall through to the audio path (the user's rule).
                return None, meta, 502, {
                    "error": "could not fetch captions",
                    "detail": tail(stderr),
                }
            else:
                # No file, no reason, no positive "none" line — ambiguous, so
                # no fallback either; say so rather than burn a minute of GPU.
                payload = no_captions_payload(meta)
                payload["detail"] += "\n\n(yt-dlp gave no reason; not transcribing locally.)"
                return None, meta, 422, payload
        else:
            try:
                with open(vtt_path, "r", encoding="utf-8", errors="replace") as handle:
                    transcript = clean_vtt(handle.read())
            except OSError as exc:
                return None, meta, 502, {
                    "error": "could not read captions", "detail": str(exc),
                }

    if not transcript.strip():
        return None, meta, 422, no_captions_payload(meta)

    transcript = truncate_middle(transcript)

    try:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"meta": meta, "transcript": transcript, "source": source},
                      handle)
    except OSError as exc:
        log("transcript cache write failed for %s: %s" % (video_id, exc))

    return transcript, meta, None, None


def transcribe_fallback(video_id, workdir, url, cookies):
    """Local whisper for a video with no caption track.

    Returns (transcript, "whisper", None) or (None, None, (status, payload)).
    Refuses (422, explained) when whisper isn't set up or the video is over
    transcribe.MAX_SECONDS; the audio never leaves workdir.
    """
    meta = read_meta(workdir)
    duration = read_duration(workdir)
    model = transcribe.available()
    if model is None:
        payload = no_captions_payload(meta)
        payload["detail"] += "\n\nLocal transcription is off: " + transcribe.unavailable_reason()
        return None, None, (422, payload)
    if duration is not None and duration > transcribe.MAX_SECONDS:
        payload = no_captions_payload(meta)
        payload["detail"] += (
            "\n\nLocal transcription skipped: video is %d min, the cap is %d min "
            "(YT_WHISPER_MAX_SECONDS)." % (duration // 60, transcribe.MAX_SECONDS // 60))
        return None, None, (422, payload)

    log("no captions for %s — transcribing locally (%s)" % (video_id, Path(model).name))

    def on_progress(stage, pct, note):
        progress_set(video_id, stage, pct, note, source="whisper")

    text, error = transcribe.transcribe(workdir, url, on_progress, cookies or None, model)
    if text is None:
        return None, None, (502, {
            "error": "no captions; local transcription failed",
            "detail": tail(error),
        })
    return text, "whisper", None


# --------------------------------------------------------------------------
# Summarization
# --------------------------------------------------------------------------

def build_prompt(meta, transcript):
    return (
        current_prompt()
        + "\n\nVideo metadata (TITLE | CHANNEL | DURATION):\n"
        + (meta or "Unknown | Unknown | Unknown")
        + "\n\nTranscript:\n"
        + transcript
        + "\n"
    )


def summarize(meta, transcript):
    """Invoke claude non-interactively with tools off (claude_text.py), pinned
    to CLAUDE_MODEL. Returns (summary, error, detail)."""
    prompt = build_prompt(meta, transcript)
    try:
        summary = llm.run(prompt, model=CLAUDE_MODEL, timeout=CLAUDE_TIMEOUT,
                          on_error="raise")
    except RuntimeError as exc:
        # llm.run folds timeout / bad-exit / no-claude into one RuntimeError whose
        # message carries the stderr tail; surface it as the detail line.
        return None, "summarizer failed", str(exc)

    if not summary:
        return None, "summarizer returned nothing", ""
    return summary, None, None


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------

def summarize_video(video_id):
    """Full pipeline for one video id.

    Returns (status_code, payload_dict).
    """
    url = canonical_url(video_id)

    cached = cache_read(video_id)
    if cached is not None:
        cached["cached"] = True
        return 200, cached

    try:
        transcript, meta, err_status, err_payload = fetch_transcript(video_id)
        if transcript is None:
            return err_status, err_payload

        source = transcript_source(video_id)
        progress_set(video_id, "summarize", None, "summarizing", source=source)
        summary, error, detail = summarize(meta, transcript)
        if summary is None:
            return 502, {"error": error, "detail": tail(detail or "")}

        payload = {
            "video_id": video_id,
            "url": url,
            "meta": meta,
            "summary": summary,
            "source": source,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        cache_write(video_id, payload)
        return 200, payload
    finally:
        progress_clear(video_id)


def transcript_source(video_id):
    """'whisper' when the cached transcript came from local speech-to-text."""
    try:
        with open(transcript_path(video_id), "r", encoding="utf-8") as handle:
            return json.load(handle).get("source") or "captions"
    except (OSError, ValueError):
        return "captions"


def chat_video(video_id):
    """Prepare a briefing for one video and open an interactive Claude on it.

    Returns (status_code, payload_dict). The summary comes from the ordinary
    pipeline and the transcript from the transcript cache — both instant when
    this video has been summarized or chatted about before.
    """
    url = canonical_url(video_id)

    status, payload = summarize_video(video_id)
    if status != 200:
        return status, payload

    transcript, meta, err_status, err_payload = fetch_transcript(video_id)
    if transcript is None:
        return err_status, err_payload

    title = meta or payload.get("meta") or video_id
    brief = build_briefing(video_id, url, title, payload.get("summary") or "", transcript)

    try:
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        brief_path = CHAT_DIR / (video_id + ".md")
        brief_path.write_text(brief, encoding="utf-8")
    except OSError as exc:
        return 500, {"error": "could not write the briefing", "detail": str(exc)}

    ok, detail = open_in_iterm(brief_path)
    if not ok:
        return 502, {"error": "could not open iTerm2", "detail": tail(detail)}

    return 200, {
        "video_id": video_id,
        "url": url,
        "meta": title,
        "briefing": str(brief_path),
    }


def ask_video(video_id, question, history):
    """Answer one question about a video, inside the bucket page.

    history is the conversation so far: a list of {"role": "user"|"assistant",
    "text": ...} dicts (may be empty). Stateless — the page holds the thread,
    the server just answers the next turn. Returns (status_code, payload).
    """
    status, payload = summarize_video(video_id)
    if status != 200:
        return status, payload

    transcript, meta, err_status, err_payload = fetch_transcript(video_id)
    if transcript is None:
        return err_status, err_payload

    title = meta or payload.get("meta") or video_id

    parts = [
        ASK_INSTRUCTIONS,
        "\nVideo metadata (TITLE | CHANNEL | DURATION):\n" + title,
        "\nSummary:\n" + (payload.get("summary") or "(none)"),
        "\nFull transcript:\n" + transcript,
    ]
    turns = [t for t in history if isinstance(t, dict)
             and t.get("role") in ("user", "assistant")
             and isinstance(t.get("text"), str) and t["text"].strip()]
    turns = turns[-ASK_HISTORY_TURNS:]
    if turns:
        parts.append("\nConversation so far:")
        for turn in turns:
            who = "Question" if turn["role"] == "user" else "Answer"
            parts.append("%s: %s" % (who, turn["text"].strip()))
    parts.append("\nFinal question: %s\n" % question.strip())
    prompt = "\n".join(parts)

    try:
        reply = llm.run(prompt, model=CLAUDE_MODEL, timeout=CLAUDE_TIMEOUT,
                        on_error="raise")
    except RuntimeError as exc:
        return 502, {"error": "claude failed", "detail": tail(str(exc))}
    if not reply:
        return 502, {"error": "claude returned nothing", "detail": ""}

    return 200, {"video_id": video_id, "reply": reply}


def build_briefing(video_id, url, title, summary, transcript):
    """The file the terminal Claude reads. Plain markdown, no front matter."""
    return (
        "# %s\n\n"
        "- **video:** %s\n"
        "- **id:** %s\n\n"
        "## Summary\n\n%s\n\n"
        "## Full transcript\n\n"
        "(Auto-generated captions: no speaker labels, and proper nouns are\n"
        "often mangled. Read through the errors.)\n\n%s\n"
        % (title, url, video_id, summary.strip() or "_(none)_", truncate_middle(transcript))
    )


def open_in_iterm(brief_path):
    """Launch tools/chat.sh in a new iTerm2 window. Returns (ok, detail)."""
    launcher = HERE.parent / "tools" / "chat.sh"
    if not launcher.exists():
        return False, "missing launcher: %s" % launcher

    command = "%s %s" % (shlex.quote(str(launcher)), shlex.quote(str(brief_path)))
    # AppleScript string literals only need the backslash and quote escaped;
    # shlex.quote above has already made the command itself shell-safe.
    literal = command.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "iTerm"\n'
        "  activate\n"
        '  create window with default profile command "%s"\n'
        "end tell\n" % literal
    )

    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=ITERM_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "osascript did not finish within %ds" % ITERM_TIMEOUT
    except OSError as exc:
        return False, str(exc)

    if proc.returncode != 0:
        return False, proc.stderr.decode("utf-8", "replace")
    return True, ""


def tail(text, limit=500):
    text = (text or "").strip()
    return text[-limit:]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def log(message):
    stamp = datetime.now().strftime("%H:%M:%S")
    print("[%s] %s" % (stamp, message), flush=True)


# Who may talk to the helper. It listens on loopback only, but a loopback port
# is reachable from every web page open in the browser — so each request must
# also prove where it came from.
#   Host   — must name this machine. Stops DNS rebinding (a hostile domain
#            re-pointed at 127.0.0.1 still sends its own name as Host).
#   Origin — browsers attach it to every cross-site request and to every
#            POST/DELETE. Absent = not a cross-site browser call (curl, a typed
#            URL, the bucket page's own GETs). Present = must be the bucket
#            page or a browser extension; a web page cannot forge it.
#   Sec-Fetch-* — the one browser call that carries no Origin is a cross-site
#            <img>/<script> tag (mode "no-cors"). It cannot read the answer,
#            but it has no business here either.
LOCAL_HOSTS = ("localhost:%d" % PORT, "127.0.0.1:%d" % PORT)
LOCAL_ORIGINS = tuple("http://" + host for host in LOCAL_HOSTS)
EXTENSION_SCHEMES = ("chrome-extension://", "safari-web-extension://",
                     "moz-extension://")


def origin_allowed(origin):
    return origin in LOCAL_ORIGINS or origin.startswith(EXTENSION_SCHEMES)


def stranger_reason(host, origin, fetch_site=None, fetch_mode=None):
    """None when the request is one of ours, else why it is refused."""
    if (host or "").lower() not in LOCAL_HOSTS:
        return "bad Host header"
    if origin is not None and not origin_allowed(origin):
        return "origin not allowed"
    if origin is None and fetch_site == "cross-site" and fetch_mode == "no-cors":
        return "cross-site embed"
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "yt-ext/0.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        # Silence BaseHTTPRequestHandler's own stderr logging; we log our own.
        pass

    def send_cors(self):
        # Never "*": echo the caller's origin back only when it is one of ours.
        origin = self.headers.get("Origin")
        if origin and origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def refuse_stranger(self):
        """The gate every verb passes first. True = a 403 went out, stop."""
        reason = stranger_reason(self.headers.get("Host"), self.headers.get("Origin"),
                                 self.headers.get("Sec-Fetch-Site"),
                                 self.headers.get("Sec-Fetch-Mode"))
        if reason is None:
            return False
        self.respond(403, {"error": "forbidden", "detail": reason})
        log("%s %s -> 403 (%s)" % (self.command, self.path, reason))
        return True

    def respond(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def respond_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- verbs ------------------------------------------------------------

    def do_OPTIONS(self):
        if self.refuse_stranger():
            return
        self.send_response(204)
        self.send_cors()
        self.end_headers()
        log("OPTIONS %s -> 204" % self.path)

    def do_GET(self):
        if self.refuse_stranger():
            return
        route = self.path.split("?")[0]

        if route == "/health":
            self.respond(200, {"ok": True})
            log("GET /health -> 200")
            return

        if route == "/":
            try:
                with open(INDEX_HTML, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.respond_bytes(
                    404, "text/plain; charset=utf-8",
                    ("bucket page missing: %s\n" % INDEX_HTML).encode("utf-8"),
                )
                log("GET / -> 404 (no index.html)")
                return
            self.respond_bytes(200, "text/html; charset=utf-8", body)
            log("GET / -> 200 (%d bytes)" % len(body))
            return

        if route == "/shared.js":
            try:
                with open(SHARED_JS, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.respond_bytes(
                    404, "text/plain; charset=utf-8",
                    ("shared module missing: %s\n" % SHARED_JS).encode("utf-8"),
                )
                log("GET /shared.js -> 404 (no shared.js)")
                return
            self.respond_bytes(
                200, "application/javascript; charset=utf-8", body
            )
            log("GET /shared.js -> 200 (%d bytes)" % len(body))
            return

        if route.startswith("/progress/"):
            video_id = unquote(route[len("/progress/"):])
            self.respond(200, progress_get(video_id))
            return

        if route == "/recent":
            items = cache_list()
            self.respond(200, items)
            log("GET /recent -> 200 (%d)" % len(items))
            return

        if route == "/config":
            self.respond(200, config_payload())
            log("GET /config -> 200")
            return

        if route == "/transcript":
            params = parse_qs(urlsplit(self.path).query)
            video_id = (params.get("v") or [""])[0]
            if not re.fullmatch(VIDEO_ID, video_id):
                self.respond(400, {"error": "not a video id"})
                log("GET /transcript -> 400 (bad id)")
                return
            transcript, meta, err_status, err_payload = fetch_transcript(video_id)
            if transcript is None:
                self.respond(err_status, err_payload)
                log("GET /transcript %s -> %d" % (video_id, err_status))
                return
            head = (meta + "\n" if meta else "") + canonical_url(video_id)
            body = (head + "\n\n" + transcript + "\n").encode("utf-8")
            self.respond_bytes(200, "text/plain; charset=utf-8", body)
            log("GET /transcript %s -> 200 (%d bytes)" % (video_id, len(body)))
            return

        self.respond(404, {"error": "not found"})
        log("GET %s -> 404" % self.path)

    def do_DELETE(self):
        if self.refuse_stranger():
            return
        route = self.path.split("?")[0]
        prefix = "/summary/"

        if not route.startswith(prefix):
            self.respond(404, {"error": "not found"})
            log("DELETE %s -> 404" % self.path)
            return

        video_id = unquote(route[len(prefix):])
        # Never let a path fragment reach the filesystem — ids are 11 chars of
        # a known alphabet or nothing at all.
        if not re.fullmatch(VIDEO_ID, video_id):
            self.respond(400, {"error": "not a video id"})
            log("DELETE %s -> 400 (bad id)" % self.path)
            return

        if not cache_delete(video_id):
            self.respond(404, {"error": "not cached"})
            log("DELETE /summary/%s -> 404" % video_id)
            return

        self.respond(200, {"ok": True, "video_id": video_id})
        log("DELETE /summary/%s -> 200" % video_id)

    def do_POST(self):
        if self.refuse_stranger():
            return
        route = self.path.split("?")[0]
        if route not in ("/summarize", "/chat", "/ask", "/config"):
            self.respond(404, {"error": "not found"})
            log("POST %s -> 404" % self.path)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""

        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except (ValueError, AttributeError):
            self.respond(400, {"error": "invalid JSON body"})
            log("POST %s -> 400 (bad json)" % route)
            return

        if route == "/config":
            if not set_prompt(body.get("prompt")):
                self.respond(500, {"error": "could not write config"})
                log("POST /config -> 500")
                return
            payload = config_payload()
            self.respond(200, payload)
            log("POST /config -> 200 (%s prompt)"
                % ("default" if payload["is_default"] else "custom"))
            return

        # The remaining routes all name one video — by url, or by bare id
        # ( /ask from the bucket page, which holds ids, not urls).
        video_id = extract_video_id(body.get("url"))
        if video_id is None:
            candidate = body.get("id")
            if isinstance(candidate, str) and re.fullmatch(VIDEO_ID, candidate):
                video_id = candidate
        if video_id is None:
            self.respond(400, {"error": "not a YouTube video URL"})
            log("POST %s -> 400 (bad url: %r)" % (route, body.get("url")))
            return

        if route == "/ask":
            question = body.get("question")
            if not isinstance(question, str) or not question.strip():
                self.respond(400, {"error": "missing question"})
                log("POST /ask %s -> 400 (no question)" % video_id)
                return
            question = question.strip()[:ASK_MAX_QUESTION]
            history = body.get("history")
            if not isinstance(history, list):
                history = []
            run = lambda vid: ask_video(vid, question, history)  # noqa: E731
        else:
            run = summarize_video if route == "/summarize" else chat_video

        started = time.time()
        log("POST %s %s ..." % (route, video_id))
        try:
            status, payload = run(video_id)
        except Exception as exc:  # last-resort guard; never kill the server
            self.respond(500, {"error": "internal error", "detail": str(exc)})
            log("POST %s %s -> 500 (%s)" % (route, video_id, exc))
            return

        self.respond(status, payload)
        log(
            "POST %s %s -> %d (%.1fs%s)"
            % (route, video_id, status, time.time() - started,
               ", cached" if payload.get("cached") else "")
        )


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log("yt-ext helper listening on http://%s:%d" % (HOST, PORT))
    log("yt-dlp: %s" % YT_DLP)
    log("claude: %s (model %s, tools off)" % (llm.claude_bin(), CLAUDE_MODEL))
    log("cache:  %s" % CACHE_DIR)
    log("bucket: http://%s:%d/ (%s)" % (HOST, PORT, INDEX_HTML))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.server_close()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
