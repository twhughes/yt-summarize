#!/usr/bin/env python3
"""Whisper fallback rules — fires on a real 'no captions' only, never on 429.

    python3 helper/test_fallback.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server           # noqa: E402
import transcribe       # noqa: E402
import captions         # noqa: E402

FAILS = []
KEEP = []      # TemporaryDirectory handles — alive until exit


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def run_case(name, captions_result, whisper_result=("hello world", ""), model="m.bin",
             duration=480):
    """Drive fetch_transcript with stubbed yt-dlp/whisper; return (status, payload, called)."""
    calls = []
    tmp = tempfile.TemporaryDirectory()
    KEEP.append(tmp)
    server.TRANSCRIPT_DIR = Path(tmp.name) / "transcripts"
    server.transcript_path = lambda vid: server.TRANSCRIPT_DIR / (vid + ".json")
    server.config_read = lambda: {}

    def fake_fetch(workdir, url, cookies=None):
        Path(workdir, "meta.txt").write_text("T | C | 8:00")
        Path(workdir, "duration.txt").write_text(str(duration))
        vtt, stderr = captions_result
        if vtt:
            Path(workdir, "cap.en.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhi there\n")
            return Path(workdir, "cap.en.vtt"), "T | C | 8:00", ""
        return None, "T | C | 8:00", stderr

    def fake_transcribe(workdir, url, on_progress, cookies=None, model=None):
        calls.append(url)
        on_progress("transcribe", 50, "transcribing locally")
        return whisper_result

    server.fetch_captions = fake_fetch
    transcribe.transcribe = fake_transcribe
    transcribe.available = lambda: model
    transcribe.unavailable_reason = lambda: "no model"
    t, meta, st, payload = server.fetch_transcript("vid123")
    return t, st, payload, calls


# 1. captions present -> no whisper
t, st, payload, calls = run_case("captions", (True, ""))
check("captions present: transcript used, whisper not called", t == "hi there" and not calls)
check("captions present: cached source is captions",
      json.load(open(server.transcript_path("vid123")))["source"] == "captions")

# 2. 429 -> 502, whisper NOT called
t, st, payload, calls = run_case("429", (None, "HTTP Error 429: Too Many Requests"))
check("429: 502 and whisper not called", t is None and st == 502 and not calls)

# 3. timeout -> 502, whisper NOT called
t, st, payload, calls = run_case("timeout", (None, "yt-dlp timed out after 120s"))
check("timeout: 502 and whisper not called", st == 502 and not calls)

# 4. real no captions (positive line from yt-dlp) -> whisper called
NONE = captions.NO_CAPTIONS_HINT + "\nWARNING: [youtube] unable to extract yt initial data"
t, st, payload, calls = run_case("nocaps", (None, NONE))
check("no captions: whisper called once and text used", t == "hello world" and calls == ["https://www.youtube.com/watch?v=vid123"])
check("no captions: cached source is whisper",
      json.load(open(server.transcript_path("vid123")))["source"] == "whisper")
check("no captions: progress carries source=whisper",
      server.progress_get("vid123").get("source") == "whisper")
server.progress_clear("vid123")

# 5. no captions but whisper unavailable -> 422 with reason
t, st, payload, calls = run_case("nomodel", (None, NONE), model=None)
check("no model: 422, explains, whisper not called",
      st == 422 and "Local transcription is off" in payload["detail"] and not calls)

# 6. too long -> 422, skipped
t, st, payload, calls = run_case("long", (None, NONE), duration=transcribe.MAX_SECONDS + 1)
check("over cap: 422, skipped, whisper not called",
      st == 422 and "skipped" in payload["detail"] and not calls)

# 7. whisper fails -> 502 with its detail
t, st, payload, calls = run_case("whfail", (None, NONE), whisper_result=(None, "whisper-cli failed\nboom"))
check("whisper failure: 502 with detail", st == 502 and "boom" in payload["detail"])

# 8. no file, no reason, no positive line -> 422, whisper NOT called
t, st, payload, calls = run_case("ambiguous", (None, ""))
check("ambiguous (empty stderr): 422 and whisper not called", st == 422 and not calls)

# 9. warnings only, no positive line -> 502, whisper NOT called
t, st, payload, calls = run_case("warn", (None, "WARNING: [youtube] something changed"))
check("warning-only stderr: 502 and whisper not called", st == 502 and not calls)

print("\n%d/%d passed" % (11 - len(FAILS), 11))
sys.exit(1 if FAILS else 0)
