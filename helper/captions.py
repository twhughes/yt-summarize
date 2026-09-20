#!/usr/bin/env python3
"""Caption fetching and VTT cleaning — the one yt-dlp/WebVTT implementation in the HQ.

Split out of helper/server.py so the transcript engine can be used without
importing (and starting) a web server. Python 3 standard library only; no state,
no side effects at import time beyond locating the yt-dlp binary.

    fetch_captions(workdir, url) -> (vtt_path | None, meta_line, last_stderr)
    clean_vtt(text)              -> plain-text transcript

`yt` is the canonical home of this code. Other HQ projects that need transcripts
(currently `morning`) load this file by explicit path rather than copying it —
see the sharing rule in ../../CLAUDE.md.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

YT_DLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"
YT_DLP_TIMEOUT = 120       # seconds, per yt-dlp attempt

# Opt-in only: name of a browser whose YouTube cookies yt-dlp may send
# (e.g. "safari", "brave"). YouTube rate-limits anonymous auto-caption
# downloads per IP (HTTP 429, seen 2026-09-08); a logged-in session is the
# maintainers' documented fix. Never defaulted on — the user flips it himself.
COOKIES_FROM_BROWSER = os.environ.get("YT_COOKIES_FROM_BROWSER", "").strip() or None

RATE_LIMIT_HINT = (
    "YouTube is rate-limiting anonymous caption downloads from this IP "
    "(HTTP 429). It usually clears on its own within hours; retry later, "
    "or set YT_COOKIES_FROM_BROWSER=safari to fetch with a logged-in session."
)


def is_rate_limited(stderr):
    """True when yt-dlp's stderr shows YouTube's caption-endpoint 429."""
    return "429" in stderr and "Too Many Requests" in stderr


# yt-dlp's positive "this video has no caption track" line (stdout, [info]).
# Only this — never an empty stderr, never a WARNING — means "no captions":
# yt-dlp emits transient WARNINGs (page-layout changes, PO-token notes) that
# must not be read as either a failure or an absence.
NO_SUBS_LINE = "There are no subtitles for the requested languages"
NO_CAPTIONS_HINT = "YouTube has no caption track for this video."


def is_no_captions(stderr):
    """True when fetch_captions established the video has no caption track."""
    return stderr.startswith(NO_CAPTIONS_HINT)


# --------------------------------------------------------------------------
# yt-dlp: metadata + captions
# --------------------------------------------------------------------------

def run_yt_dlp(workdir, url, extra_args, cookies_from_browser=None):
    """Run one yt-dlp attempt in workdir. Returns the CompletedProcess."""
    cmd = [
        YT_DLP,
        "--skip-download",
        "--sub-format", "vtt",
        "--print-to-file", "%(title)s | %(channel)s | %(duration_string)s", "meta.txt",
        "--print-to-file", "%(duration)s", "duration.txt",
        "-o", "cap",
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    cmd += extra_args + [url]
    return subprocess.run(
        cmd,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=YT_DLP_TIMEOUT,
    )


def find_vtt(workdir):
    """Return the path of the first .vtt file yt-dlp produced, if any."""
    preferred = Path(workdir) / "cap.en.vtt"
    if preferred.exists():
        return preferred
    matches = sorted(Path(workdir).glob("*.vtt"))
    return matches[0] if matches else None


def fetch_captions(workdir, url, cookies_from_browser=COOKIES_FROM_BROWSER):
    """Try progressively looser caption options.

    Returns (vtt_path, meta_line, last_stderr). vtt_path is None on failure.
    A 429 from YouTube's caption endpoint ends the attempts at once — the
    looser options hit the same endpoint, and each extra hit deepens the
    throttle (verified 2026-09-09: same 429 across every client/format).
    """
    attempts = [
        ["--write-auto-subs", "--sub-lang", "en"],
        ["--write-subs", "--sub-lang", "en"],
        ["--write-auto-subs", "--write-subs", "--sub-lang", "en.*"],
    ]

    last_stderr = ""
    said_none = 0            # attempts whose stdout carried NO_SUBS_LINE
    for extra_args in attempts:
        try:
            proc = run_yt_dlp(workdir, url, extra_args, cookies_from_browser)
        except subprocess.TimeoutExpired:
            last_stderr = "yt-dlp timed out after %ds" % YT_DLP_TIMEOUT
            continue
        except OSError as exc:
            last_stderr = "could not run yt-dlp: %s" % exc
            break

        last_stderr = proc.stderr.decode("utf-8", "replace")
        vtt = find_vtt(workdir)
        if vtt is not None:
            return vtt, read_meta(workdir), last_stderr
        if is_rate_limited(last_stderr):
            last_stderr = RATE_LIMIT_HINT + "\n" + last_stderr
            break
        if NO_SUBS_LINE in proc.stdout.decode("utf-8", "replace"):
            said_none += 1

    if said_none == len(attempts):
        # Every attempt ran to completion and yt-dlp said so each time.
        last_stderr = NO_CAPTIONS_HINT + "\n" + last_stderr
    return None, read_meta(workdir), last_stderr


def read_meta(workdir):
    """Read the 'title | channel | duration' line yt-dlp printed, if present."""
    path = Path(workdir) / "meta.txt"
    if not path.exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def read_duration(workdir):
    """Seconds yt-dlp reported for the video, or None if unknown."""
    path = Path(workdir) / "duration.txt"
    try:
        return int(float(path.read_text(encoding="utf-8").strip().splitlines()[0]))
    except (OSError, IndexError, ValueError):
        return None


# --------------------------------------------------------------------------
# VTT cleaning
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]*>")
HEADER_PREFIXES = ("WEBVTT", "Kind:", "Language:", "NOTE ", "STYLE", "REGION")


def clean_vtt(text):
    """Turn a WebVTT caption file into a plain-text transcript.

    Drops cue timings and headers, strips inline tags, and collapses the
    rolling-caption duplication that YouTube auto-subs are full of.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "-->" in line:
            continue
        if line.startswith(HEADER_PREFIXES):
            continue
        # Bare cue-number lines ("12") carry no content.
        if line.isdigit():
            continue
        line = TAG_RE.sub("", line).strip()
        if not line:
            continue
        # Rolling captions repeat the previous line verbatim; drop consecutive dupes.
        if lines and lines[-1] == line:
            continue
        lines.append(line)

    return " ".join(lines)
