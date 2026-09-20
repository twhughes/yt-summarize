// YT Summarize — background service worker.
//
// Owns ALL state: a queue of videos to summarize, processed strictly one at a
// time, plus the set of tabs whose panel is open. Content scripts are dumb
// renderers; they get told the whole state and draw it. Both are mirrored to
// storage.local on every change so they survive the service worker being
// killed.
//
// The panel is opt-in per tab. It appears only where one of the three
// triggers fired — toolbar button, right-click a video page, right-click a
// link — and closing it in a tab keeps it closed there. Plain YouTube
// browsing never summons it.

importScripts("shared.js"); // -> globalThis.YT (URL parsing; see shared.js)

const b = globalThis.browser ?? globalThis.chrome;

const { videoId, watchUrl } = YT;

const HELPER_PROGRESS = "http://localhost:8188/progress";
const PROGRESS_POLL_MS = 1000;
const BUCKET = "http://localhost:8188/";
const OEMBED = "https://www.youtube.com/oembed";
const STORAGE_KEY = "ytSummarizeQueue";
const PANEL_TABS_KEY = "ytSummarizePanelTabs";

const TITLE_TIMEOUT_MS = 2000;

const HELPER_DOWN_MESSAGE = "helper not running — run ./run.sh in a terminal";

// Pages/links we are willing to act on.
const PAGE_PATTERNS = [
  "*://*.youtube.com/watch*",
  "*://*.youtube.com/shorts/*",
  "*://*.youtube.com/live/*"
];

const LINK_PATTERNS = PAGE_PATTERNS.concat([
  "*://youtu.be/*"
]);

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/**
 * @typedef {{
 *   id: string, url: string, title: string,
 *   status: "queued"|"running"|"done"|"error",
 *   summary: string|null, error: string|null, addedAt: number
 * }} Item
 */

/** @type {Item[]} */
let items = [];
/** @type {Set<number>} Tabs whose panel is open. */
let openTabs = new Set();
let restored = null; // Promise — the one-time restore from storage.
let pumping = false;
let lastStamp = 0;

// addedAt doubles as the sort key, so two videos queued in the same
// millisecond must still get distinct, increasing values.
function stamp() {
  const now = Date.now();
  lastStamp = now > lastStamp ? now : lastStamp + 1;
  return lastStamp;
}

const YOUTUBE_PAGE = /^https?:\/\/([a-z0-9-]+\.)?youtube\.com\//i;

// Only a YouTube tab has a content script, so only a YouTube tab can show the
// panel. Everywhere else the bucket page stands in for it.
function isYouTubeTab(tab) {
  return !!(tab && typeof tab.url === "string" && YOUTUBE_PAGE.test(tab.url));
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

function ensureRestored() {
  if (!restored) {
    restored = restore();
  }
  return restored;
}

async function restore() {
  let bag = null;
  try {
    bag = await b.storage.local.get([STORAGE_KEY, PANEL_TABS_KEY]);
  } catch (err) {
    bag = null;
  }

  // The worker dies after ~30s idle; without this a woken worker would think
  // every panel was closed and hide them mid-summary.
  const tabs = bag && bag[PANEL_TABS_KEY];
  if (Array.isArray(tabs)) {
    openTabs = new Set(tabs.filter((id) => typeof id === "number"));
  }

  const stored = bag && bag[STORAGE_KEY];
  if (!Array.isArray(stored)) {
    return;
  }

  items = stored
    .filter((item) => item && typeof item.id === "string")
    .map((item) => ({
      id: item.id,
      url: item.url || watchUrl(item.id),
      title: item.title || `video ${item.id}`,
      // Anything mid-flight when the worker died gets retried.
      status: item.status === "running" ? "queued" : (item.status || "queued"),
      summary: item.summary || null,
      error: item.error || null,
      addedAt: typeof item.addedAt === "number" ? item.addedAt : stamp()
    }));

  for (const item of items) {
    lastStamp = Math.max(lastStamp, item.addedAt);
  }
}

async function persist() {
  try {
    await b.storage.local.set({
      [STORAGE_KEY]: items,
      [PANEL_TABS_KEY]: Array.from(openTabs)
    });
  } catch (err) {
    // Storage full or unavailable — the in-memory queue still works.
  }
}

// ---------------------------------------------------------------------------
// Panel visibility — per tab, never global
// ---------------------------------------------------------------------------

// Returns true only when a live content script confirmed it drew the panel.
// In Safari a tab whose document predates this extension instance is
// unreachable — its content world is still wired to the dead instance, and
// both sendMessage and executeScript into it "succeed" silently. The caller
// needs the truth so it can fall back to the bucket page.
async function openPanel(tabId, focusId) {
  if (tabId == null) {
    return false;
  }
  openTabs.add(tabId);
  await persist();
  await broadcast(focusId ? { tab: tabId, id: focusId } : null);
  if (await alive(tabId)) {
    return true;
  }
  openTabs.delete(tabId);
  await persist();
  return false;
}

// A content script that answers ping is provably from THIS extension
// instance; anything else (error, or Safari's silent undefined) is dead air.
async function alive(tabId) {
  try {
    const reply = await b.tabs.sendMessage(tabId, { type: "ping" });
    return !!(reply && reply.ok);
  } catch (err) {
    return false;
  }
}

async function closePanel(tabId) {
  if (tabId == null || !openTabs.delete(tabId)) {
    return;
  }
  await persist();
  await broadcast();
}

// Tab ids are recycled across browser restarts and extension reloads, so a
// remembered id could pop the panel open on an unrelated tab. Start clean.
async function forgetPanels() {
  await ensureRestored();
  if (!openTabs.size) {
    return;
  }
  openTabs.clear();
  await persist();
}

// An extension reload leaves the previous content script frozen in every open
// YouTube tab — panel still on screen, nothing behind it, and no way to reach
// the new instance. Re-injecting heals Chrome. In Safari it does NOT: the
// injected copy runs in the old instance's content world (or one with no
// extension API at all) and stays deaf, while every API call reports success.
// The only reconnect Safari offers is a real page reload, so a tab that still
// won't answer ping after injection gets reloaded. This only runs from
// onInstalled — i.e. after a rebuild/update — never during normal browsing.
async function sweepOrphans() {
  let tabs = [];
  try {
    tabs = await b.tabs.query({ url: "*://*.youtube.com/*" });
  } catch (err) {
    return;
  }
  await Promise.all(tabs.map(async (tab) => {
    if (tab.id == null) {
      return;
    }
    if (await alive(tab.id)) {
      return; // Manifest injection already connected this document.
    }
    try {
      await b.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["/shared.js", "/content.js"]
      });
    } catch (err) {
      // Mid-navigation or restricted — the reload below sorts it out too.
    }
    if (await alive(tab.id)) {
      return; // Chrome: the injected copy answers.
    }
    try {
      await b.tabs.reload(tab.id);
    } catch (err) {
      // Tab vanished; nothing to heal.
    }
  }));
}

// ---------------------------------------------------------------------------
// Broadcast
// ---------------------------------------------------------------------------

function snapshot() {
  return items
    .slice()
    .sort((x, y) => y.addedAt - x.addedAt)
    .map((item) => ({
      id: item.id,
      url: item.url,
      title: item.title,
      status: item.status,
      summary: item.summary,
      source: item.source || null,
      progress: item.status === "running" ? (item.progress || null) : null,
      error: item.error,
      addedAt: item.addedAt
    }));
}

async function changed() {
  await persist();
  await broadcast();
}

// `focus` is {tab, id} — the one tab that just triggered, and the row it
// wants expanded. Every other tab gets the state with no opinion about it.
async function broadcast(focus) {
  const state = snapshot();

  await badge();

  let tabs = [];
  try {
    tabs = await b.tabs.query({ url: "*://*.youtube.com/*" });
  } catch (err) {
    tabs = [];
  }

  await Promise.all(tabs.map(async (tab) => {
    if (tab.id == null) {
      return;
    }
    const message = {
      type: "state",
      items: state,
      open: openTabs.has(tab.id),
      focus: focus && focus.tab === tab.id ? focus.id : null
    };
    try {
      // Safari resolves sendMessage with undefined when there is no listener
      // (Chrome rejects) — so a missing {ok:true} reply is also a failure.
      const reply = await b.tabs.sendMessage(tab.id, message);
      if (!reply || !reply.ok) {
        throw new Error("no live content script");
      }
    } catch (err) {
      // No content script yet — normal right after an install or reload for
      // tabs that were already open. Inject it, but only where it has to draw.
      if (message.open) {
        await injectAndSend(tab.id, message);
      }
    }
  }));
}

async function injectAndSend(tabId, message) {
  try {
    await b.scripting.executeScript({
      target: { tabId },
      files: ["/shared.js", "/content.js"]
    });
    await b.tabs.sendMessage(tabId, message);
  } catch (err) {
    // Restricted page, or the tab went away. Nothing to do.
  }
}

async function badge() {
  const pending = items.filter(
    (item) => item.status === "queued" || item.status === "running"
  ).length;
  try {
    await b.action.setBadgeText({ text: pending ? String(pending) : "" });
  } catch (err) {
    // Safari can refuse before the toolbar item exists; harmless.
  }
}

// ---------------------------------------------------------------------------
// Enqueue
// ---------------------------------------------------------------------------

// NOTE: enqueueing never navigates the tab you are on. Clicking a context
// menu item does not follow the link by itself, and nothing here changes
// tab.url — only openBucket() opens anything, and only in its own tab.
// Returns the video id (already-known ones included) or null.
async function enqueue(rawUrl) {
  await ensureRestored();

  const id = videoId(rawUrl);
  if (!id) {
    return null;
  }

  // Already queued, running, done (cache hit) or errored — the caller just
  // wants it shown.
  if (items.some((item) => item.id === id)) {
    return id;
  }

  const item = {
    id,
    url: watchUrl(id),
    title: `video ${id}`,
    status: "queued",
    summary: null,
    error: null,
    addedAt: stamp()
  };
  items.push(item);
  await changed();

  // Title and pump run on their own time — the caller wants the id now so the
  // panel can open without waiting on the network.
  fetchTitle(id).then(async (title) => {
    // The row may have been removed while we waited for the title.
    if (title && items.includes(item)) {
      item.title = title;
      await changed();
    }
  });

  pump();
  return id;
}

// ---------------------------------------------------------------------------
// The three entry points all land here
// ---------------------------------------------------------------------------

// Queue a video and put it in front of the user: the in-page panel when he is on
// YouTube, the bucket page when he is anywhere else.
async function trigger(rawUrl, tab) {
  const id = await enqueue(rawUrl);
  if (!id) {
    return;
  }
  // A YouTube tab gets the in-page panel — but only if a live content script
  // confirms it drew (Safari tabs from a previous extension instance can't).
  // Anywhere the panel can't appear, the bucket page shows the summary
  // instead; a trigger must never dissolve into nothing visible.
  if (isYouTubeTab(tab) && tab.id != null && (await openPanel(tab.id, id))) {
    return;
  }
  await broadcast();
  await openBucket(id);
}

// The helper's localhost page — the panel's off-YouTube counterpart, and the
// archive of everything ever summarized. #v=<id> tells it what to open;
// #chat=<id> additionally unfolds the chat pane on that video.
async function openBucket(id, kind) {
  const target = id ? `${BUCKET}#${kind || "v"}=${id}` : BUCKET;

  let existing = [];
  try {
    existing = await b.tabs.query({
      url: ["http://localhost:8188/*", "http://127.0.0.1:8188/*"]
    });
  } catch (err) {
    existing = [];
  }

  try {
    const tab = existing[0];
    if (tab && tab.id != null) {
      await b.tabs.update(tab.id, { url: target, active: true });
    } else {
      await b.tabs.create({ url: target });
    }
  } catch (err) {
    // Popup blocked or the helper is down; the queue still holds the video.
  }
}

// Entry point 4 — chat about the video inside the bucket page. All the work
// (summarize if needed, transcript fetch, the conversation itself) happens
// there; this end just opens the right deep link. The old iTerm2 route still
// exists as the bucket page's "Open in terminal" button.
async function chat(rawUrl) {
  const id = videoId(rawUrl);
  if (!id) {
    return;
  }
  await openBucket(id, "chat");
}

async function fetchTitle(id) {
  const target = `${OEMBED}?url=${encodeURIComponent(watchUrl(id))}&format=json`;
  const control = new AbortController();
  const timer = setTimeout(() => control.abort(), TITLE_TIMEOUT_MS);
  try {
    const response = await fetch(target, { signal: control.signal });
    if (!response.ok) {
      return null;
    }
    const payload = await response.json();
    return (payload && typeof payload.title === "string" && payload.title) || null;
  } catch (err) {
    return null; // Best effort only — the placeholder title stays.
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// The pump — one summarize at a time, FIFO
// ---------------------------------------------------------------------------

async function pump() {
  if (pumping) {
    return;
  }
  pumping = true;
  try {
    await ensureRestored();
    for (;;) {
      const next = items
        .filter((item) => item.status === "queued")
        .sort((x, y) => x.addedAt - y.addedAt)[0];
      if (!next) {
        return;
      }

      next.status = "running";
      next.error = null;
      next.progress = null;
      await changed();

      const stopWatching = watchProgress(next);
      try {
        const payload = await callHelper(next.url);
        next.summary = payload.summary;
        next.source = payload.source || "captions";
        next.status = "done";
        next.error = null;
      } catch (err) {
        next.error = describe(err);
        next.status = "error";
      } finally {
        stopWatching();
        next.progress = null;
      }
      await changed();
    }
  } finally {
    pumping = false;
  }
}

// Poll the helper's progress for the running item and push it to the panels
// as it changes. Progress is ephemeral: broadcast only, never persisted.
function watchProgress(item) {
  let stopped = false;
  let last = "";
  const timer = setInterval(async () => {
    if (stopped || item.status !== "running") {
      return;
    }
    try {
      const response = await fetch(HELPER_PROGRESS, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: item.id })
      });
      const progress = await response.json();
      if (stopped || item.status !== "running") {
        return;
      }
      item.progress = progress && progress.stage ? progress : null;
      const key = JSON.stringify(item.progress);
      if (key !== last) {
        last = key;
        await broadcast();
      }
    } catch (err) {
      // Helper hiccup — the bar just stays where it was.
    }
  }, PROGRESS_POLL_MS);
  return () => {
    stopped = true;
    clearInterval(timer);
  };
}

async function callHelper(url) {
  try {
    return await YT.requestSummary("http://localhost:8188", url);
  } catch (err) {
    if (err instanceof TypeError) throw new Error(HELPER_DOWN_MESSAGE);
    throw err;
  }
}

function describe(err) {
  return (err && err.message) ? err.message : String(err);
}

// ---------------------------------------------------------------------------
// Menus + entry points
// ---------------------------------------------------------------------------

b.runtime.onInstalled.addListener(() => {
  createMenus();
  forgetPanels()
    .then(() => sweepOrphans())
    .then(() => pump());
});

if (b.runtime.onStartup) {
  b.runtime.onStartup.addListener(() => {
    createMenus();
    forgetPanels().then(() => pump());
  });
}

const MENU_ITEMS = [
  // Entry point 2 — right-click the video you are watching.
  {
    id: "yt-sum-page",
    title: "Summarize this video",
    contexts: ["page", "video", "frame"],
    documentUrlPatterns: PAGE_PATTERNS
  },
  // Entry point 3 — right-click any YouTube link, anywhere.
  {
    id: "yt-sum-link",
    title: "Summarize this video link",
    contexts: ["link"],
    targetUrlPatterns: LINK_PATTERNS
  },
  // Entry point 4 — open the bucket page's chat on this video. On both the
  // page you are watching and any YouTube link (the user's ask 2026-08-21: the
  // link flow is "summarize or chat about it without opening it").
  {
    id: "yt-chat",
    title: "Chat about this video",
    contexts: ["page", "video", "frame"],
    documentUrlPatterns: PAGE_PATTERNS
  },
  {
    id: "yt-chat-link",
    title: "Chat about this video link",
    contexts: ["link"],
    targetUrlPatterns: LINK_PATTERNS
  }
];

// Safari fires onInstalled AND onStartup on a single extension reload, and —
// unlike Chrome, which rejects a duplicate id — it silently accepts the second
// registration. Two fire-and-forget createMenus() calls therefore both awaited
// removeAll(), both saw an empty menu, and both created the same rows: the
// menu showed every item twice, with only one copy wired to a live listener
// (the other did nothing when clicked). Serializing the rebuild is the fix —
// two runs can no longer interleave between the remove and the create.
let menuWork = Promise.resolve();

function createMenus() {
  menuWork = menuWork.then(rebuildMenus, rebuildMenus);
  return menuWork;
}

async function rebuildMenus() {
  // removeAll is promise-based in Safari/Firefox and callback-or-promise in
  // Chrome MV3 — await covers both. Failing here must not skip create().
  try {
    await b.contextMenus.removeAll();
  } catch (err) {
    // Nothing to remove, or the callback flavour resolved oddly. Carry on.
  }

  for (const item of MENU_ITEMS) {
    // Belt and braces: removeAll on a freshly-woken worker has been seen to
    // miss rows a previous instance owned, so drop each id by name too.
    try {
      await b.contextMenus.remove(item.id);
    } catch (err) {
      // Not there — which is the normal case.
    }
    try {
      // The callback swallows Chrome's duplicate-id runtime.lastError, which
      // is otherwise reported as an unchecked error on every rebuild.
      b.contextMenus.create(item, () => void b.runtime.lastError);
    } catch (err) {
      // Safari throws synchronously on some duplicates instead. Same answer.
    }
  }
}

b.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "yt-chat" || info.menuItemId === "yt-chat-link") {
    const url = info.menuItemId === "yt-chat-link" ? info.linkUrl : (tab && tab.url);
    chat(url).catch(() => {});
    return;
  }
  const url = info.menuItemId === "yt-sum-link" ? info.linkUrl : (tab && tab.url);
  trigger(url, tab).catch(() => {});
});

// Entry point 1 — the toolbar button, which reads the tab it was clicked on:
// on a video it summarizes, elsewhere on YouTube it is the panel's on/off
// switch, and off YouTube it opens the bucket page.
b.action.onClicked.addListener((tab) => {
  toolbar(tab).catch(() => {});
});

async function toolbar(tab) {
  await ensureRestored();

  if (videoId(tab && tab.url)) {
    await trigger(tab.url, tab);
    return;
  }

  if (isYouTubeTab(tab) && tab.id != null) {
    if (openTabs.has(tab.id)) {
      await closePanel(tab.id);
    } else if (!(await openPanel(tab.id, null))) {
      await openBucket(null); // Unreachable tab (see openPanel) — show the bucket.
    }
    return;
  }

  await openBucket(null);
}

// A closed tab's panel state is meaningless, and a tab that has left YouTube
// should not find the panel waiting when it comes back.
if (b.tabs.onRemoved) {
  b.tabs.onRemoved.addListener((tabId) => {
    if (openTabs.delete(tabId)) {
      persist();
    }
  });
}

if (b.tabs.onUpdated) {
  b.tabs.onUpdated.addListener((tabId, info, tab) => {
    if (!openTabs.has(tabId)) {
      return;
    }
    const url = info.url || (tab && tab.url);
    if (!url || YOUTUBE_PAGE.test(url)) {
      return;
    }
    openTabs.delete(tabId);
    persist();
  });
}

// ---------------------------------------------------------------------------
// Messages from content scripts
// ---------------------------------------------------------------------------

b.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.type) {
    return false;
  }

  const tabId = sender && sender.tab ? sender.tab.id : null;

  switch (message.type) {
    case "getState":
      ensureRestored()
        .then(() => {
          sendResponse({
            type: "state",
            items: snapshot(),
            open: tabId != null && openTabs.has(tabId),
            focus: null
          });
          badge();
          pump(); // A woken worker may have queued work waiting.
        })
        .catch(() => sendResponse({ type: "state", items: [], open: false }));
      return true;

    case "closePanel":
      ensureRestored()
        .then(async () => {
          await closePanel(tabId);
          sendResponse({ ok: true });
        })
        .catch(() => sendResponse({ ok: false }));
      return true;

    case "summarize":
      ensureRestored()
        .then(async () => {
          await trigger(message.url, sender && sender.tab);
          sendResponse({ ok: true });
        })
        .catch(() => sendResponse({ ok: false }));
      return true;

    case "chat":
      // The panel's "Chat about this video" button — same landing as the
      // context-menu items: the bucket page's chat pane.
      chat(watchUrl(message.id))
        .then(() => sendResponse({ ok: true }))
        .catch(() => sendResponse({ ok: false }));
      return true;

    case "remove":
      ensureRestored()
        .then(async () => {
          items = items.filter((item) => item.id !== message.id);
          await changed();
          sendResponse({ ok: true });
        })
        .catch(() => sendResponse({ ok: false }));
      return true;

    case "clearDone":
      ensureRestored()
        .then(async () => {
          items = items.filter((item) => item.status !== "done");
          await changed();
          sendResponse({ ok: true });
        })
        .catch(() => sendResponse({ ok: false }));
      return true;

    default:
      return false;
  }
});

// A fresh worker (wake from idle, browser restart) resumes anything pending.
ensureRestored().then(() => {
  badge();
  pump();
});
