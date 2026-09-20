// YT Summarize — the bits both halves of the tool need.
//
// This file is the single source of truth for two things that used to be
// copy-pasted: what counts as a YouTube video URL, and how a summary becomes
// DOM. It is loaded three ways, so it must stay dependency-free and must not
// touch `document` at load time:
//
//   extension background  importScripts("shared.js")   (no DOM at all)
//   extension content     manifest content_scripts, before content.js
//   bucket page           <script src="/shared.js">    (served by helper/server.py
//                                                       straight from this file)
//
// It publishes one global, `YT`. Loading it twice is harmless.

globalThis.YT = (() => {
  const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;

  function okId(candidate) {
    return candidate && VIDEO_ID_RE.test(candidate) ? candidate : null;
  }

  // Extract an 11-char video id from any YouTube URL shape we support.
  // Returns null for playlists, channels, the home page, or anything else.
  function videoId(rawUrl) {
    if (!rawUrl || typeof rawUrl !== "string") {
      return null;
    }

    let url;
    try {
      url = new URL(rawUrl.trim());
    } catch (err) {
      return null;
    }

    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return null;
    }

    const host = url.hostname.toLowerCase().replace(/^www\./, "");
    const parts = url.pathname.split("/").filter(Boolean);

    // youtu.be/<id>
    if (host === "youtu.be") {
      return okId(parts[0]);
    }

    if (host !== "youtube.com" && !host.endsWith(".youtube.com")) {
      return null;
    }

    // youtube.com/watch?v=<id>  (also the m. and music. subdomains)
    if (parts[0] === "watch") {
      return okId(url.searchParams.get("v"));
    }

    // youtube.com/shorts/<id>, /live/<id>, /embed/<id>, /v/<id>
    if (["shorts", "live", "embed", "v"].includes(parts[0])) {
      return okId(parts[1]);
    }

    return null;
  }

  function watchUrl(id) {
    return "https://www.youtube.com/watch?v=" + id;
  }

  // A blob of pasted/dropped text -> {ids: [...], rejects: [...]}.
  function parseDrop(text) {
    const ids = [];
    const rejects = [];
    const seen = new Set();

    for (const token of String(text || "").split(/\s+/)) {
      if (!token) {
        continue;
      }
      const id = videoId(token);
      if (id) {
        if (!seen.has(id)) {
          seen.add(id);
          ids.push(id);
        }
      } else {
        rejects.push(token);
      }
    }

    return { ids, rejects };
  }

  // Status -> the character and the modifier both renderers use. Each side
  // keeps its own class prefix; only the mapping is shared.
  function glyph(status) {
    if (status === "running") return { char: "◌", kind: "run" };
    if (status === "done") return { char: "✓", kind: "done" };
    if (status === "error") return { char: "⚠", kind: "err" };
    return { char: "◷", kind: "" };
  }

  // The helper's GET /progress/<id> body -> what a row should show while it
  // runs. `pct` is null for stages with no percentage (captions, summarize),
  // which the renderers draw as an indeterminate bar. `whisper` is true when
  // the local speech-to-text fallback is doing the work — the row should say
  // so plainly, that's the whole point of the bar.
  function progressView(progress) {
    const p = progress || {};
    const whisper = p.source === "whisper";
    const pct = typeof p.pct === "number" ? Math.max(0, Math.min(100, p.pct)) : null;
    let label;
    if (!p.stage) {
      label = "Summarizing…";
    } else if (p.stage === "captions") {
      label = "Fetching captions…";
    } else if (p.stage === "download") {
      label = "No captions — downloading audio…";
    } else if (p.stage === "convert") {
      label = "No captions — converting audio…";
    } else if (p.stage === "transcribe") {
      label = "No captions — transcribing locally…";
    } else if (p.stage === "summarize") {
      label = whisper ? "Transcribed locally — summarizing…" : "Summarizing…";
    } else {
      label = p.note || "Working…";
    }
    if (pct !== null) {
      label += " " + pct + "%";
    }
    return { label, pct, whisper };
  }

  // Short badge for a finished row whose transcript came from local whisper
  // rather than YouTube's caption track; "" for the normal case.
  function sourceBadge(source) {
    return source === "whisper" ? "🎙 local transcript" : "";
  }

  async function requestSummary(base, url) {
    const post = async (route, body) => {
      const control = new AbortController();
      const timer = setTimeout(() => control.abort(), 15000);
      try {
        const response = await fetch(base + route, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body), signal: control.signal
        });
        const data = await response.json();
        if (!response.ok) {
          throw new Error((data.error || "Helper request failed") +
            (data.detail ? "\n\n" + data.detail : ""));
        }
        return { status: response.status, data };
      } finally { clearTimeout(timer); }
    };
    let result = await post("/summarize", { url });
    const id = result.data.video_id;
    while (result.status === 202) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      result = await post("/job", { id });
    }
    if (!result.data.summary) throw new Error("Helper returned an empty summary");
    return result.data;
  }

  const LABEL_MAX = 32;

  // "Core claim: the thing" -> <strong>Core claim:</strong> the thing.
  // Anything without a short leading label is written out plainly, which is
  // what old pre-format cached summaries get.
  function labelled(target, text) {
    const cut = text.indexOf(": ");
    if (cut > 0 && cut <= LABEL_MAX) {
      const strong = document.createElement("strong");
      strong.textContent = text.slice(0, cut + 1);
      target.appendChild(strong);
      target.appendChild(document.createTextNode(text.slice(cut + 1)));
      return target;
    }
    target.textContent = text;
    return target;
  }

  // Render a summary into `target`, which the caller has already given its own
  // class. The two renderers differ only in the heading level and whether they
  // print the source URL, so those are options rather than separate copies.
  //
  // Model output only ever reaches the DOM through textContent — never
  // innerHTML. Keep it that way.
  function fillSummary(target, summary, options) {
    const opts = options || {};
    const headingTag = opts.headingTag || "h4";

    let headingDone = false;
    let list = null;

    for (const raw of String(summary == null ? "" : summary).split("\n")) {
      const line = raw.trim();

      if (!line) {
        list = null;
        continue;
      }

      if (!headingDone) {
        // First non-empty line is TITLE | CHANNEL | DURATION.
        const heading = document.createElement(headingTag);
        heading.textContent = line;
        target.appendChild(heading);
        headingDone = true;
        continue;
      }

      if (line.startsWith("- ") || line.startsWith("* ")) {
        if (!list) {
          list = document.createElement("ul");
          target.appendChild(list);
        }
        const li = document.createElement("li");
        labelled(li, line.slice(2).trim());
        list.appendChild(li);
        continue;
      }

      list = null;
      const para = document.createElement("p");
      if (/^bottom line:\s/i.test(line)) {
        labelled(para, line);
      } else {
        para.textContent = line;
      }
      target.appendChild(para);
    }

    if (opts.sourceUrl) {
      const src = document.createElement("div");
      if (opts.sourceClass) {
        src.className = opts.sourceClass;
      }
      const link = document.createElement("a");
      link.href = opts.sourceUrl;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = opts.sourceUrl;
      src.appendChild(link);
      target.appendChild(src);
    }

    return target;
  }

  return {
    requestSummary,
    VIDEO_ID_RE,
    videoId,
    watchUrl,
    parseDrop,
    glyph,
    progressView,
    sourceBadge,
    labelled,
    fillSummary
  };
})();
