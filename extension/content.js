// YT Summarize — content script.
//
// A dumb renderer. All state lives in the background worker — including
// whether this tab's panel is open. This file draws whatever it is told and
// sends back user intents (summarize / remove / clearDone / closePanel).
// Everything is built with createElement + textContent; model output never
// touches innerHTML.
//
// It rides along on every YouTube page but shows nothing until the background
// says this tab was explicitly triggered. Browsing YouTube must stay quiet.

(() => {
  const b = globalThis.browser ?? globalThis.chrome;

  const PANEL_ID = "yt-ext-panel";
  const STYLE_ID = "yt-ext-style";

  // content.js ships in the manifest AND gets injected when the background
  // finds a tab with no live listener — every extension reload orphans the
  // copy in an already-open tab. Retire the previous instance rather than
  // bailing out; bailing would leave the tab with a dead script and no panel
  // until it was reloaded by hand.
  if (typeof globalThis.__ytSummarizeTeardown === "function") {
    try {
      globalThis.__ytSummarizeTeardown();
    } catch (err) {
      // Orphaned instance — its listeners died with the old extension context.
    }
  }

  let panel = null;
  let listEl = null;
  let countEl = null;
  let actionEl = null;

  let latest = [];             // Newest-first, as sent by the background.
  let open = false;            // The background owns this; the tab obeys.
  let lastStatus = new Map();  // id -> last status, for done-transitions.
  let expandedId = null;
  let expandedManually = false;
  let watcher = null;          // SPA-nav poll, alive only while open.
  let watchedHref = "";

  // The panel needs the id locally to label its button and match it against a
  // row. Same parser the background uses — see shared.js.
  function currentVideoId() {
    return YT.videoId(location.href);
  }

  // -------------------------------------------------------------------------
  // Chrome
  // -------------------------------------------------------------------------

  const CSS = `
#yt-ext-panel {
  position: fixed;
  top: 16px;
  right: 16px;
  width: 440px;
  max-width: calc(100vw - 32px);
  max-height: 75vh;
  overflow-y: auto;
  z-index: 2147483647;
  background: #1c1c1e;
  color: #ffffff;
  font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  line-height: 1.5;
  border-radius: 12px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
  padding: 12px 14px 10px;
  box-sizing: border-box;
  text-align: left;
}
#yt-ext-panel * { box-sizing: border-box; }
.yt-ext-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.yt-ext-title {
  font-weight: 600;
  font-size: 12px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  opacity: 0.6;
  white-space: nowrap;
}
.yt-ext-count {
  font-size: 12px;
  opacity: 0.5;
  flex: 1 1 auto;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.yt-ext-btn {
  background: transparent;
  border: none;
  color: #ffffff;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  opacity: 0.55;
  padding: 2px 4px;
  border-radius: 6px;
}
.yt-ext-btn:hover { opacity: 1; background: rgba(255, 255, 255, 0.1); }
.yt-ext-x { font-size: 18px; line-height: 1; }
.yt-ext-action {
  display: block;
  width: 100%;
  margin: 0 0 8px;
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 8px;
  color: #ffffff;
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.yt-ext-action:hover { background: rgba(255, 255, 255, 0.16); }
.yt-ext-chatbtn {
  display: inline-block;
  margin-top: 8px;
  padding: 4px 10px;
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 7px;
  color: #ffffff;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  opacity: 0.65;
}
.yt-ext-chatbtn:hover { opacity: 1; background: rgba(255, 255, 255, 0.1); }
.yt-ext-empty { opacity: 0.5; font-size: 13px; padding: 6px 2px 8px; }
.yt-ext-row {
  border-top: 1px solid rgba(255, 255, 255, 0.1);
  padding: 7px 0 6px;
}
.yt-ext-line {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.yt-ext-glyph {
  width: 16px;
  flex: 0 0 16px;
  text-align: center;
  font-size: 13px;
  opacity: 0.85;
}
.yt-ext-glyph.yt-ext-done { color: #4ade80; }
.yt-ext-glyph.yt-ext-err { color: #ff6b6b; }
.yt-ext-glyph.yt-ext-run {
  display: inline-block;
  animation: yt-ext-spin 1.1s linear infinite;
  color: #7dd3fc;
}
@keyframes yt-ext-spin { to { transform: rotate(360deg); } }
.yt-ext-name {
  flex: 1 1 auto;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 13px;
}
.yt-ext-row.yt-ext-open .yt-ext-name { white-space: normal; }
.yt-ext-detail {
  margin: 6px 0 4px 24px;
  font-size: 13px;
  opacity: 0.92;
}
.yt-ext-detail h4 {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 700;
}
.yt-ext-detail p { margin: 0 0 8px; }
.yt-ext-detail ul { margin: 6px 0; padding-left: 20px; }
.yt-ext-detail li { margin-bottom: 5px; }
.yt-ext-detail strong { font-weight: 650; }
.yt-ext-detail.yt-ext-errtext { color: #ff9c9c; white-space: pre-wrap; }
.yt-ext-prog {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 5px 0 2px 24px;
  font-size: 11.5px;
  opacity: 0.8;
}
.yt-ext-prog .yt-ext-bar {
  flex: 0 0 120px;
  height: 4px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.14);
  overflow: hidden;
}
.yt-ext-prog .yt-ext-bar i {
  display: block;
  height: 100%;
  width: 0;
  background: #7dd3fc;
  transition: width 0.4s ease;
}
.yt-ext-prog.yt-ext-whisper .yt-ext-bar i { background: #fbbf24; }
.yt-ext-prog .yt-ext-bar.yt-ext-indet i {
  width: 35%;
  animation: yt-ext-slide 1.3s ease-in-out infinite;
}
@keyframes yt-ext-slide { 0% { margin-left: 0; } 50% { margin-left: 65%; } 100% { margin-left: 0; } }
.yt-ext-badge {
  font-size: 10.5px;
  opacity: 0.6;
  white-space: nowrap;
  flex: 0 0 auto;
}
`;

  function ensurePanel() {
    if (!document.getElementById(STYLE_ID)) {
      const style = document.createElement("style");
      style.id = STYLE_ID;
      style.textContent = CSS;
      (document.head || document.documentElement).appendChild(style);
    }

    if (panel && panel.isConnected) {
      return panel;
    }

    panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.style.display = "none"; // render() decides when it earns its place.

    const head = document.createElement("div");
    head.className = "yt-ext-head";

    const title = document.createElement("div");
    title.className = "yt-ext-title";
    title.textContent = "YT Summarize";

    countEl = document.createElement("div");
    countEl.className = "yt-ext-count";

    const clear = document.createElement("button");
    clear.className = "yt-ext-btn";
    clear.textContent = "Clear done";
    clear.addEventListener("click", (event) => {
      event.stopPropagation();
      send({ type: "clearDone" });
    });

    const close = document.createElement("button");
    close.className = "yt-ext-btn yt-ext-x";
    close.textContent = "×";
    close.setAttribute("aria-label", "Close panel");
    close.addEventListener("click", (event) => {
      event.stopPropagation();
      dismiss();
    });

    head.appendChild(title);
    head.appendChild(countEl);
    head.appendChild(clear);
    head.appendChild(close);

    // Only on a video page: summarize what you're watching without leaving
    // the panel (SPA navigation retargets it as you browse).
    actionEl = document.createElement("button");
    actionEl.className = "yt-ext-action";
    actionEl.style.display = "none";
    actionEl.addEventListener("click", (event) => {
      event.stopPropagation();
      const id = actionEl.dataset.videoId;
      if (!id) {
        return;
      }
      if (latest.some((item) => item.id === id)) {
        expandedId = id;
        expandedManually = true;
        render();
        scrollToRow(id);
        return;
      }
      send({ type: "summarize", url: location.href });
    });

    listEl = document.createElement("div");

    panel.appendChild(head);
    panel.appendChild(actionEl);
    panel.appendChild(listEl);
    // documentElement, not body — YouTube's SPA swaps big chunks of the body.
    document.documentElement.appendChild(panel);

    return panel;
  }

  // -------------------------------------------------------------------------
  // Open / closed
  // -------------------------------------------------------------------------

  function setOpen(next) {
    open = !!next;
    if (open) {
      ensurePanel();
      startWatching();
    } else {
      stopWatching();
    }
    render();
  }

  // Closing is a decision, not a per-page accident: the background remembers
  // it for this tab so browsing on does not bring the panel back.
  function dismiss() {
    open = false;
    stopWatching();
    if (panel) {
      panel.style.display = "none";
    }
    send({ type: "closePanel" });
  }

  // YouTube is a single-page app, so the "this video" button has to notice
  // navigation. yt-navigate-finish covers the normal case; the poll covers
  // the rest.
  function startWatching() {
    if (watcher) {
      return;
    }
    watchedHref = location.href;
    watcher = setInterval(() => {
      if (location.href === watchedHref) {
        return;
      }
      watchedHref = location.href;
      render();
    }, 1000);
    document.addEventListener("yt-navigate-finish", onNavigate);
  }

  function stopWatching() {
    if (!watcher) {
      return;
    }
    clearInterval(watcher);
    watcher = null;
    document.removeEventListener("yt-navigate-finish", onNavigate);
  }

  function onNavigate() {
    watchedHref = location.href;
    render();
  }

  function scrollToRow(id) {
    if (!listEl) {
      return;
    }
    const el = listEl.querySelector('[data-id="' + id + '"]');
    if (el && el.scrollIntoView) {
      el.scrollIntoView({ block: "nearest" });
    }
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  function render() {
    if (!open) {
      if (panel) {
        panel.style.display = "none";
      }
      return;
    }

    ensurePanel();
    panel.style.display = "block";

    const running = latest.filter((item) => item.status === "running").length;
    const queued = latest.filter((item) => item.status === "queued").length;
    countEl.textContent = running || queued
      ? `${running} running · ${queued} queued`
      : "";

    renderAction();

    while (listEl.firstChild) {
      listEl.removeChild(listEl.firstChild);
    }

    if (!latest.length) {
      const empty = document.createElement("div");
      empty.className = "yt-ext-empty";
      empty.textContent = "Nothing queued. Right-click a video or link → Summarize.";
      listEl.appendChild(empty);
      return;
    }

    for (const item of latest) {
      listEl.appendChild(row(item));
    }
  }

  function renderAction() {
    const id = currentVideoId();
    if (!id) {
      actionEl.style.display = "none";
      actionEl.dataset.videoId = "";
      return;
    }
    actionEl.style.display = "block";
    actionEl.dataset.videoId = id;
    actionEl.textContent = latest.some((item) => item.id === id)
      ? "Show this video's summary"
      : "Summarize this video";
  }

  function row(item) {
    const expanded = expandedId === item.id;

    const wrap = document.createElement("div");
    wrap.className = expanded ? "yt-ext-row yt-ext-open" : "yt-ext-row";
    wrap.dataset.id = item.id;

    const line = document.createElement("div");
    line.className = "yt-ext-line";

    const mark = YT.glyph(item.status);
    const glyph = document.createElement("span");
    glyph.className = "yt-ext-glyph";
    if (mark.kind) {
      glyph.classList.add("yt-ext-" + mark.kind);
    }
    glyph.textContent = mark.char;

    const name = document.createElement("div");
    name.className = "yt-ext-name";
    name.textContent = item.title || item.url || item.id;
    name.title = item.title || item.id;

    const kill = document.createElement("button");
    kill.className = "yt-ext-btn yt-ext-x";
    kill.textContent = "×";
    kill.setAttribute("aria-label", "Remove");
    kill.addEventListener("click", (event) => {
      event.stopPropagation();
      if (expandedId === item.id) {
        expandedId = null;
        expandedManually = false;
      }
      send({ type: "remove", id: item.id });
    });

    line.appendChild(glyph);
    line.appendChild(name);
    const badgeText = item.status === "done" ? YT.sourceBadge(item.source) : "";
    if (badgeText) {
      const badge = document.createElement("span");
      badge.className = "yt-ext-badge";
      badge.textContent = badgeText;
      badge.title = "YouTube had no caption track; the audio was transcribed on this Mac with whisper.";
      line.appendChild(badge);
    }
    line.appendChild(kill);
    line.addEventListener("click", () => {
      if (expandedId === item.id) {
        expandedId = null;
        expandedManually = false;
      } else {
        expandedId = item.id;
        expandedManually = true;
      }
      render();
    });

    wrap.appendChild(line);

    if (item.status === "running") {
      const view = YT.progressView(item.progress);
      const box = document.createElement("div");
      box.className = "yt-ext-prog" + (view.whisper ? " yt-ext-whisper" : "");
      const bar = document.createElement("span");
      bar.className = "yt-ext-bar" + (view.pct === null ? " yt-ext-indet" : "");
      const fill = document.createElement("i");
      if (view.pct !== null) {
        fill.style.width = view.pct + "%";
      }
      bar.appendChild(fill);
      const text = document.createElement("span");
      text.textContent = view.label;
      box.appendChild(bar);
      box.appendChild(text);
      wrap.appendChild(box);
    }

    if (expanded) {
      if (item.status === "error") {
        const detail = document.createElement("div");
        detail.className = "yt-ext-detail yt-ext-errtext";
        detail.textContent = item.error || "Unknown error.";
        wrap.appendChild(detail);
      } else if (item.summary) {
        const box = document.createElement("div");
        box.className = "yt-ext-detail";
        YT.fillSummary(box, item.summary, { headingTag: "h4" });

        // Hand off to the bucket page's chat pane on this video.
        const chatBtn = document.createElement("button");
        chatBtn.className = "yt-ext-chatbtn";
        chatBtn.type = "button";
        chatBtn.textContent = "💬 Chat about this video";
        chatBtn.addEventListener("click", (event) => {
          event.stopPropagation();
          send({ type: "chat", id: item.id });
        });
        box.appendChild(chatBtn);

        wrap.appendChild(box);
      } else {
        const detail = document.createElement("div");
        detail.className = "yt-ext-detail";
        detail.textContent =
          item.status === "running" ? "Summarizing…" : "Waiting in the queue…";
        wrap.appendChild(detail);
      }
    }

    return wrap;
  }

  // -------------------------------------------------------------------------
  // State intake
  // -------------------------------------------------------------------------

  function apply(message) {
    latest = Array.isArray(message.items) ? message.items : [];
    const ids = new Set(latest.map((item) => item.id));

    // A trigger names the video it wants read — that beats any other choice.
    if (message.focus && ids.has(message.focus)) {
      expandedId = message.focus;
      expandedManually = true;
    }

    // Auto-expand a freshly finished summary, but never steal an expansion
    // the user chose themselves.
    for (const item of latest) {
      const before = lastStatus.get(item.id);
      if (item.status === "done" && before && before !== "done") {
        if (!expandedManually || !ids.has(expandedId)) {
          expandedId = item.id;
          expandedManually = false;
        }
      }
    }

    lastStatus = new Map(latest.map((item) => [item.id, item.status]));

    if (expandedId && !ids.has(expandedId)) {
      expandedId = null;
      expandedManually = false;
    }

    setOpen(message.open);

    if (message.focus && ids.has(message.focus)) {
      scrollToRow(message.focus);
    }
  }

  function send(message) {
    try {
      const reply = b.runtime.sendMessage(message);
      if (reply && typeof reply.catch === "function") {
        reply.catch(() => {});
      }
    } catch (err) {
      // Worker restarting; the next broadcast will straighten us out.
    }
  }

  // -------------------------------------------------------------------------
  // Wiring
  // -------------------------------------------------------------------------

  function onMessage(message, sender, sendResponse) {
    if (!message || !message.type) {
      return false;
    }
    if (message.type === "ping") {
      sendResponse({ ok: true });
      return false;
    }
    if (message.type === "state") {
      apply(message);
      sendResponse({ ok: true });
      return false;
    }
    return false;
  }

  // Hover + keystroke fast path (the user, 2026-08-21): ⌥S summarizes, ⌥C opens
  // chat — acting on the link under the cursor if there is one, else on the
  // video being watched. Physical-key codes (KeyS/KeyC), because on macOS
  // Option changes event.key ("ß"). Silent when neither target exists.
  let hoverUrl = null;

  function onHover(event) {
    const link = event.target && event.target.closest
      ? event.target.closest("a[href]")
      : null;
    hoverUrl = link ? link.href : null;
  }

  function typingInto(el) {
    if (!el || !el.tagName) {
      return false;
    }
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
      || el.isContentEditable;
  }

  function onKeydown(event) {
    if (event.key === "Escape" && open) {
      dismiss();
      return;
    }
    if (!event.altKey || event.metaKey || event.ctrlKey) {
      return;
    }
    if (event.code !== "KeyS" && event.code !== "KeyC") {
      return;
    }
    if (typingInto(event.target) || typingInto(document.activeElement)) {
      return;
    }
    const url = (hoverUrl && YT.videoId(hoverUrl))
      ? hoverUrl
      : (currentVideoId() ? location.href : null);
    if (!url) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (event.code === "KeyS") {
      send({ type: "summarize", url: url });
    } else {
      send({ type: "chat", id: YT.videoId(url) });
    }
  }

  b.runtime.onMessage.addListener(onMessage);
  // Capture phase, so ⌥S/⌥C beat YouTube's own document-level key handling.
  document.addEventListener("keydown", onKeydown, true);
  document.addEventListener("mouseover", onHover, true);

  globalThis.__ytSummarizeTeardown = () => {
    stopWatching();
    document.removeEventListener("keydown", onKeydown, true);
    document.removeEventListener("mouseover", onHover, true);
    try {
      b.runtime.onMessage.removeListener(onMessage);
    } catch (err) {
      // Already-dead extension context; nothing left to detach from.
    }
    for (const id of [PANEL_ID, STYLE_ID]) {
      const stale = document.getElementById(id);
      if (stale) {
        stale.remove();
      }
    }
    panel = null;
  };

  function askForState() {
    let pending;
    try {
      pending = b.runtime.sendMessage({ type: "getState" });
    } catch (err) {
      return;
    }
    if (pending && typeof pending.then === "function") {
      pending.then((reply) => {
        if (reply && reply.type === "state") {
          apply(reply);
        }
      }).catch(() => {});
    } else {
      // Chrome's callback flavour.
      try {
        b.runtime.sendMessage({ type: "getState" }, (reply) => {
          if (reply && reply.type === "state") {
            apply(reply);
          }
        });
      } catch (err) {
        // Nothing to do.
      }
    }
  }

  // No ensurePanel() here on purpose — nothing touches the page until the
  // background says this tab asked for the panel.
  askForState();
})();
