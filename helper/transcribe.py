#!/usr/bin/env python3
"""Local speech-to-text fallback for videos YouTube serves no captions for.

Pipeline, all local, all in one throwaway workdir that the caller deletes:

    yt-dlp (bestaudio)  ->  ffmpeg (16 kHz mono wav)  ->  whisper-cli  ->  text

Fires ONLY when the caption path reported "no captions" (a real 422). A 429,
a timeout, or any other caption failure never reaches this module — those
are transient and the audio endpoint would just deepen the hole. Audio is
never kept: only the text leaves the workdir. Python 3 standard library.

    available()                               -> model path | None
    transcribe(workdir, url, on_progress)     -> (text | None, error_text)

on_progress(stage, pct, note) is called as the stages advance so the server
can expose a live progress bar; stage is "download" | "convert" | "transcribe".
"""

import os
import re
import shutil
import threading
import subprocess
from pathlib import Path

try:
    import processes
except ImportError:
    from . import processes

WHISPER_CLI = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# Longest video the fallback will transcribe. base.en on this Mac runs about
# 10x realtime (8:00 of audio ≈ 45 s), so an hour is a ~6 minute wait and the
# queue is strictly one-at-a-time — anything longer is a deliberate no.
MAX_SECONDS = int(os.environ.get("YT_WHISPER_MAX_SECONDS", "3600"))

DOWNLOAD_TIMEOUT = 300
CONVERT_TIMEOUT = 300
TRANSCRIBE_TIMEOUT = 1800

# Model lookup order. First hit wins. Drop a ggml *.bin into
# ~/.cache/yt-ext/models/ to override; the MacWhisper path is the one already
# on this Mac (2026-09-10) and is read-only from here.
_MODEL_DIR = Path.home() / ".cache" / "yt-ext" / "models"
_MACWHISPER = (Path.home() / "Library" / "Containers" / "com.goodsnooze.MacWhisper"
               / "Data" / "Library" / "Application Support" / "MacWhisper" / "models"
               / "ggml-model-whisper-base.en.bin")


def available():
    """Path of the whisper model to use, or None when the fallback can't run."""
    if not (os.path.exists(WHISPER_CLI) and os.path.exists(FFMPEG)):
        return None
    forced = os.environ.get("YT_WHISPER_MODEL", "").strip()
    if forced:
        return forced if os.path.exists(forced) else None
    if _MODEL_DIR.is_dir():
        models = sorted(_MODEL_DIR.glob("*.bin"))
        if models:
            return str(models[0])
    if _MACWHISPER.exists():
        return str(_MACWHISPER)
    return None


def unavailable_reason():
    """One line saying why available() is None, for the error detail."""
    if not os.path.exists(WHISPER_CLI):
        return "whisper-cli is not installed (brew install whisper-cpp)"
    if not os.path.exists(FFMPEG):
        return "ffmpeg is not installed (brew install ffmpeg)"
    return "no whisper model found (drop a ggml *.bin into ~/.cache/yt-ext/models/)"


# --------------------------------------------------------------------------

_DL_PCT = re.compile(r"\[download\]\s+([0-9.]+)%")
_WH_PCT = re.compile(r"progress\s*=\s*([0-9]+)%")


def _stream(cmd, cwd, timeout, on_line):
    """Run cmd, feeding each line of its combined output to on_line.

    Returns (returncode, tail_of_output). Raises subprocess.TimeoutExpired.
    """
    proc = processes.spawn(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1,
    )
    expired = threading.Event()
    def kill_group():
        processes.kill(proc)
    def deadline():
        expired.set()
        kill_group()
    timer = threading.Timer(timeout, deadline)
    timer.daemon = True
    timer.start()
    tail = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line:
                tail.append(line)
                tail = tail[-40:]
                on_line(line)
        code = proc.wait()
        if expired.is_set():
            raise subprocess.TimeoutExpired(cmd, timeout)
        return code, "\n".join(tail)
    except BaseException:
        kill_group()
        proc.wait()
        raise
    finally:
        timer.cancel()
        proc.stdout.close()
        processes.release(proc)



def transcribe(workdir, url, on_progress, cookies_from_browser=None, model=None):
    """Download the audio of url into workdir and run whisper on it.

    Returns (text, error). text is None on failure; error is one line plus
    the tail of the failing tool's output. Never raises for tool failures.
    """
    model = model or available()
    if model is None:
        return None, "local transcription unavailable: " + unavailable_reason()
    workdir = Path(workdir)

    # 1. audio ---------------------------------------------------------------
    on_progress("download", 0, "downloading audio")
    cmd = [
        shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp",
        "--newline", "--progress", "--no-playlist",
        "-f", "bestaudio/best",
        "-o", "audio.%(ext)s",
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    cmd.append(url)

    def dl_line(line):
        hit = _DL_PCT.search(line)
        if hit:
            on_progress("download", min(100, int(float(hit.group(1)))), "downloading audio")

    try:
        code, out = _stream(cmd, workdir, DOWNLOAD_TIMEOUT, dl_line)
    except subprocess.TimeoutExpired:
        return None, "audio download timed out after %ds" % DOWNLOAD_TIMEOUT
    except OSError as exc:
        return None, "could not run yt-dlp: %s" % exc
    audio = sorted(p for p in workdir.glob("audio.*") if p.suffix != ".wav")
    if code != 0 or not audio:
        return None, "audio download failed\n" + out

    # 2. wav -----------------------------------------------------------------
    on_progress("convert", 0, "converting audio")
    wav = workdir / "audio.wav"
    try:
        proc = processes.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", str(audio[0]),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
            cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=CONVERT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "audio conversion timed out after %ds" % CONVERT_TIMEOUT
    except OSError as exc:
        return None, "could not run ffmpeg: %s" % exc
    if proc.returncode != 0 or not wav.exists():
        return None, "audio conversion failed\n" + proc.stdout[-1500:]
    try:
        audio[0].unlink()          # the compressed original is dead weight now
    except OSError:
        pass

    # 3. whisper -------------------------------------------------------------
    on_progress("transcribe", 0, "transcribing locally")
    cmd = [WHISPER_CLI, "-m", model, "-f", str(wav),
           "-otxt", "-of", "out", "-nt", "-pp"]

    def wh_line(line):
        hit = _WH_PCT.search(line)
        if hit:
            on_progress("transcribe", min(100, int(hit.group(1))), "transcribing locally")

    try:
        code, out = _stream(cmd, workdir, TRANSCRIBE_TIMEOUT, wh_line)
    except subprocess.TimeoutExpired:
        return None, "transcription timed out after %ds" % TRANSCRIBE_TIMEOUT
    except OSError as exc:
        return None, "could not run whisper-cli: %s" % exc
    out_path = workdir / "out.txt"
    if code != 0 or not out_path.exists():
        return None, "whisper-cli failed\n" + out

    try:
        text = out_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, "could not read transcript: %s" % exc
    text = " ".join(text.split())
    if not text:
        return None, "whisper-cli produced an empty transcript"
    on_progress("transcribe", 100, "transcribed")
    return text, ""
