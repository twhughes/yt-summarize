# yt-summarize

Right-click a YouTube video in Chrome, pick **Summarize video**, and read a short summary about
10 seconds later: a TL;DR, 3 to 5 one-sentence bullets, and a bottom line.

- No API keys. It uses your own Claude plan through the `claude` command.
- Everything runs on your Mac. Nothing is stored anywhere else.
- It downloads the video's captions only, never the video.

## What you need

- A Mac with Chrome
- A paid Claude plan (Pro or Max)
- About 15 minutes

## Set it up

**1. Get the code.** On GitHub press the green **Code** button, then **Download ZIP**. Unzip it.
Open the Terminal app, type `cd ` (with the space), drag the unzipped folder into the window, and
press return.

**2. Check your Mac.**

```
./install.sh
```

It installs nothing. It lists what is missing and prints the one command that fixes each item.
Run it again until it says **All set**.

**3. Load the extension in Chrome.**

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Press **Load unpacked** and pick the `extension` folder inside this folder.

**4. Start the helper.**

```
./run.sh
```

Leave that window open. Press Ctrl-C to stop it.

## Use it

- Right-click any YouTube video page or video link, then **Summarize video**.
- Click the extension's toolbar button to show the queue on the page.
- Open <http://localhost:8188/> for the full page: paste links, read every summary you ever made,
  ask questions about a video, copy a transcript, edit the summary prompt (the ⚙ button).

## When it fails

| You see | Cause | Fix |
| --- | --- | --- |
| "helper not running" | `./run.sh` is not running | Start it again and leave the window open |
| "HTTP Error 429" | YouTube rate-limits caption downloads | Wait a few hours. Do not retry in a loop; retries make it last longer |
| "no captions" | The video has no caption track | Nothing to fix. With `whisper-cli` and `ffmpeg` installed, the helper transcribes it on your Mac |
| A sudden failure on every video | YouTube changed something | `brew upgrade yt-dlp` |
| "claude failed" | Claude is logged out or out of quota | Run `claude` once and check |

## Settings

Set these before `./run.sh`, for example `YT_EXT_MODEL=claude-opus-5 ./run.sh`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `YT_EXT_MODEL` | `claude-sonnet-5` | The Claude model that writes the summary |
| `YT_EXT_PORT` | `8188` | The helper's port. The extension expects 8188 |
| `YT_EXT_CACHE` | `~/.cache/yt-ext` | The folder that holds your summaries |

## How it stays safe

The helper is a small web server, and Claude reads text from strangers' videos. Two locks cover that:

1. **Only your own pages reach the helper.** It listens on `127.0.0.1` only, and it refuses every
   request that does not come from the extension or from its own page. A web site you visit gets
   a 403.
2. **Claude has no tools.** Every call runs as `claude -p --tools ""`. Claude cannot run commands,
   read files, or use the network. The worst a hostile transcript can do is a bad summary.

`python3 helper/test_security.py` proves both locks.

One exception to know: the **Open in terminal** button starts a normal interactive `claude` on the
transcript, in iTerm2. That session has Claude's normal tools and normal permission prompts. Do
not approve a command there that you did not ask for.

## Layout

```
extension/   the Chrome extension (Manifest V3, plain JavaScript)
helper/      the local server: captions -> clean text -> claude -> cache
run.sh       starts the helper
install.sh   checks your Mac
```

Python standard library only. No `pip install`, no `npm install`.

MIT licence. No warranty.
