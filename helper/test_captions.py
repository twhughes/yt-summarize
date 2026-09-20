#!/usr/bin/env python3
"""Tests for captions.clean_vtt. No network, no yt-dlp.

Run:  python3 test_captions.py     (from this directory)
"""

import sys
import tempfile
import types

import captions
from captions import clean_vtt

VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.120 --> 00:00:02.900 align:start position:0%
so <00:00:00.480><c>here's</c> the thing

00:00:02.900 --> 00:00:05.400
so here's the thing
about <c>rolling</c> captions

3
00:00:05.400 --> 00:00:08.000
they repeat every single line
"""

EXPECTED = (
    "so here's the thing about rolling captions "
    "they repeat every single line"
)


def check(name, got, want):
    if got != want:
        print("FAIL %s\n  got:  %r\n  want: %r" % (name, got, want))
        return False
    print("ok   %s" % name)
    return True


def rate_limit_short_circuit():
    """A 429 must end the attempts after ONE yt-dlp run, with the hint on top."""
    calls = []

    def fake_run(workdir, url, extra_args, cookies_from_browser=None):
        calls.append(extra_args)
        return types.SimpleNamespace(
            stdout=b"",
            stderr=b"ERROR: Unable to download video subtitles for 'en': "
                   b"HTTP Error 429: Too Many Requests\n")

    real = captions.run_yt_dlp
    captions.run_yt_dlp = fake_run
    try:
        with tempfile.TemporaryDirectory() as workdir:
            vtt, _meta, stderr = captions.fetch_captions(workdir, "https://youtu.be/x")
    finally:
        captions.run_yt_dlp = real
    return vtt is None and len(calls) == 1 and stderr.startswith(captions.RATE_LIMIT_HINT)


def no_caption_track_is_positive():
    """Three 'no subtitles' [info] lines (with a stray WARNING) => NO_CAPTIONS_HINT.
    A stray WARNING alone, without the [info] line, must NOT."""
    def fake_run_none(workdir, url, extra_args, cookies_from_browser=None):
        return types.SimpleNamespace(
            stdout=b"[info] There are no subtitles for the requested languages\n",
            stderr=b"WARNING: [youtube] unable to extract yt initial data\n")

    def fake_run_warn(workdir, url, extra_args, cookies_from_browser=None):
        return types.SimpleNamespace(stdout=b"", stderr=b"WARNING: [youtube] hmm\n")

    real = captions.run_yt_dlp
    try:
        captions.run_yt_dlp = fake_run_none
        with tempfile.TemporaryDirectory() as workdir:
            vtt, _m, err_none = captions.fetch_captions(workdir, "https://youtu.be/x")
        captions.run_yt_dlp = fake_run_warn
        with tempfile.TemporaryDirectory() as workdir:
            vtt2, _m, err_warn = captions.fetch_captions(workdir, "https://youtu.be/x")
    finally:
        captions.run_yt_dlp = real
    return (vtt is None and captions.is_no_captions(err_none)
            and vtt2 is None and not captions.is_no_captions(err_warn))


def main():
    results = [
        check("429 stops after one attempt + hint", rate_limit_short_circuit(), True),
        check("no-caption-track needs yt-dlp's positive line", no_caption_track_is_positive(), True),
        check("cleans a real-shaped auto-caption file", clean_vtt(VTT), EXPECTED),
        check("empty input", clean_vtt(""), ""),
        check("headers only", clean_vtt("WEBVTT\n\nKind: captions\n"), ""),
        check("strips tags", clean_vtt("<c.colorE5E5E5>hello</c> world"), "hello world"),
        check("drops cue numbers", clean_vtt("1\nreal line\n2\n"), "real line"),
    ]
    if not all(results):
        sys.exit(1)
    print("\n%d/%d passed" % (len(results), len(results)))


if __name__ == "__main__":
    main()
