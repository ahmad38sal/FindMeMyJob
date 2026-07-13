// MV3 service worker. Acts as the only thing that talks to the backend —
// content scripts and the popup ask via chrome.runtime.sendMessage so we
// don't sprinkle the bearer token / settings access across every script.
//
// Tab pinning: when match-by-url resolves a tab to a Job, we remember
// {tabId → job_id} so subsequent pages on the same tab (or new tabs opened
// from it — the apply link often targets a different host like Workday)
// stay locked to the same job. Otherwise the popup would lose context the
// moment the user clicks "Apply" and lands on the application form, whose
// URL isn't tracked.
//
// Important: we MUST return true from onMessage to keep sendResponse alive
// across async work. https://developer.chrome.com/docs/extensions/develop/concepts/messaging

import * as api from "./lib/api.js";

const BADGE = {
  good: { text: "✓", color: "#1d8a3a" },
  weak: { text: "?", color: "#b48a00" },
  none: { text: "•", color: "#888" },
  err:  { text: "!", color: "#c62828" },
};

async function setBadge(tabId, kind) {
  if (!tabId) return;
  const b = BADGE[kind];
  if (!b) {
    await chrome.action.setBadgeText({ tabId, text: "" });
    return;
  }
  await chrome.action.setBadgeText({ tabId, text: b.text });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: b.color });
}

// ----- Tab pin storage (chrome.storage.session: per-tab, browser-session lifetime) -----

const pinKey = (tabId) => `pin_${tabId}`;

async function getPin(tabId) {
  if (!tabId) return null;
  const obj = await chrome.storage.session.get(pinKey(tabId));
  return obj[pinKey(tabId)] ?? null;
}

async function setPin(tabId, jobId) {
  if (!tabId || !jobId) return;
  await chrome.storage.session.set({ [pinKey(tabId)]: jobId });
}

async function clearPin(tabId) {
  if (!tabId) return;
  await chrome.storage.session.remove(pinKey(tabId));
}

// If this tab has no pin but was opened from another tab that did, inherit it.
// Catches "Apply" links that open in a new tab.
async function getEffectivePin(tabId) {
  const own = await getPin(tabId);
  if (own) return own;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab?.openerTabId) return null;
  const inherited = await getPin(tab.openerTabId);
  if (inherited) await setPin(tabId, inherited); // promote so it survives further nav
  return inherited;
}

chrome.tabs.onRemoved.addListener((tabId) => clearPin(tabId));

// Inherit at tab-creation time as well (some pages open Apply via window.open;
// the new tab might not be the active one when the popup is opened later).
chrome.tabs.onCreated.addListener(async (tab) => {
  if (tab?.id && tab.openerTabId) {
    const inherited = await getPin(tab.openerTabId);
    if (inherited) await setPin(tab.id, inherited);
  }
});

// ----- Message handling -----

const HANDLERS = {
  "match-current-page": async ({ url, page_title, company }, sender) => {
    const tabId = sender?.tab?.id;
    // Try URL match first — if the user navigated back to a tracked listing
    // we want the live URL to win over a stale pin.
    const urlMatch = await api.matchByUrl({ url, page_title, company });
    if (urlMatch.job_id) {
      await setPin(tabId, urlMatch.job_id);
      await setBadge(tabId, (urlMatch.match_score ?? 0) >= 0.7 ? "good" : "weak");
      return { ...urlMatch, pinned: false };
    }
    // No URL match — fall back to a pin (own or inherited from opener tab).
    const pinnedJobId = tabId ? await getEffectivePin(tabId) : null;
    if (pinnedJobId) {
      const pinned = await api.matchByUrl({ job_id: pinnedJobId });
      if (pinned.job_id) {
        await setBadge(tabId, (pinned.match_score ?? 0) >= 0.7 ? "good" : "weak");
        return { ...pinned, pinned: true };
      }
      // Pin pointed at a job that no longer exists — clear it.
      await clearPin(tabId);
    }
    await setBadge(tabId, "none");
    return urlMatch; // suggest_action: track
  },
  "fetch-autofill-payload": async ({ job_id }) => {
    return api.getAutofillPayload(job_id);
  },
  "fetch-application-data": async ({ job_id }) => {
    return api.getApplicationData(job_id);
  },
  "fetch-tailored-resume": async ({ job_id }) => {
    return api.getTailoredResumeEnvelope(job_id);
  },
  "llm-fill-suggest": async (msg) => {
    // Strip the routing fields before forwarding.
    const { kind, tab_id_hint, ...body } = msg;
    return api.llmFillSuggest(body);
  },
  "list-jobs": async ({ q }) => {
    return api.listJobs(q);
  },
  // Manually pin a user-chosen tracked job to the current tab. Reuses the
  // same pin store as URL-resolved pins, so subsequent "match-current-page"
  // resolves to this job (via match-by-url job_id) — no duplicate Job is
  // created. "unpin-tab" clears it like any other pin.
  "pin-job": async ({ job_id }, sender) => {
    const tabId = sender?.tab?.id;
    if (!job_id) throw Object.assign(new Error("job_id required"), { kind: "BadRequest" });
    if (tabId) await setPin(tabId, job_id);
    const matched = await api.matchByUrl({ job_id });
    if (tabId) await setBadge(tabId, (matched.match_score ?? 0) >= 0.7 ? "good" : "weak");
    return { ...matched, pinned: true };
  },
  "track-url": async ({ url, page_title, company }, sender) => {
    const result = await api.trackUrl({ url, page_title, company });
    if (result?.job_id && sender?.tab?.id) await setPin(sender.tab.id, result.job_id);
    return result;
  },
  "unpin-tab": async (_msg, sender) => {
    const tabId = sender?.tab?.id;
    if (tabId) {
      await clearPin(tabId);
      await setBadge(tabId, null);
    }
    return { ok: true };
  },
  "get-backend-url": async () => ({ backend_url: await api.getBackendUrl() }),
};

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handler = HANDLERS[msg?.kind];
  if (!handler) {
    sendResponse({ ok: false, error: { kind: "UnknownMessage", message: `No handler for ${msg?.kind}` } });
    return false;
  }
  // The popup sends messages from a context with no sender.tab — synthesize
  // one from the popup's "active tab" hint so pinning still works there.
  const tabHint = msg?.tab_id_hint;
  const effSender = sender?.tab ? sender : { ...sender, tab: { id: tabHint } };
  (async () => {
    try {
      const data = await handler(msg, effSender);
      sendResponse({ ok: true, data });
    } catch (e) {
      const tabId = effSender?.tab?.id;
      if (tabId) await setBadge(tabId, "err");
      sendResponse({ ok: false, error: { kind: e.kind || "Error", message: e.message, status: e.status, body: e.body } });
    }
  })();
  return true; // keep the message channel open for async sendResponse
});

// Clear the badge when the user navigates away — stale badges lie. Pin
// survives navigation though (that's the whole point).
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading") {
    chrome.action.setBadgeText({ tabId, text: "" }).catch(() => {});
  }
});
