// Popup logic. Flow:
//   1. Read active tab URL/title.
//   2. Ask background to match it against the FindMeMyJob tracker. The
//      background may resolve the tab via a previously-set tab pin (e.g.
//      user pinned on the listing page, then navigated to the Workday
//      apply form which has a different URL).
//   3. Render one of: matched-with-resume / matched-no-resume / not-matched / error.
//   4. Action buttons send messages to the active tab's content script,
//      which does the actual DOM work (engine.js + upload.js).

const root = document.getElementById("root");
const statusEl = document.getElementById("status");
let activeTabId = null;

document.getElementById("open-options").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

// Background uses sender.tab.id to key tab pins. Popup messages carry no
// sender.tab, so we pass the active tab id as a hint.
function send(kind, data = {}) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ kind, tab_id_hint: activeTabId, ...data }, (resp) => resolve(resp));
  });
}

function tabSend(tabId, message) {
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tabId, message, (resp) => resolve(resp));
  });
}

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = kind;
}

function fmtScore(s) {
  if (s == null) return "—";
  return Math.round(s * 100) + "";
}

function scoreClass(s) {
  if (s == null) return "";
  if (s >= 0.7) return "good";
  if (s >= 0.4) return "weak";
  return "bad";
}

// "Software Engineer at Acme — Greenhouse" → "Acme"
function guessCompany(title, hostname) {
  const m = / at ([^|—–\-]+?)(?:\s*[\|\-—–]|$)/i.exec(title || "");
  if (m) return m[1].trim();
  if (hostname?.endsWith(".lever.co")) return hostname.split(".")[0];
  if (hostname === "boards.greenhouse.io") return null;
  return null;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function render() {
  const tab = await activeTab();
  activeTabId = tab?.id ?? null;
  if (!tab?.url) {
    root.innerHTML = `<div class="note">No active tab.</div>`;
    return;
  }
  const url = tab.url;
  const page_title = tab.title || "";
  const hostname = (() => { try { return new URL(url).hostname; } catch { return ""; } })();
  const company = guessCompany(page_title, hostname);

  const resp = await send("match-current-page", { url, page_title, company });
  if (!resp?.ok) {
    return renderError(resp?.error || { kind: "Error", message: "Unknown failure" });
  }
  return renderMatch(resp.data, tab);
}

function renderError(err) {
  const needsAuth = err.kind === "ConfigError" || err.kind === "AuthError";
  root.innerHTML = `
    <div class="error"><strong>${err.kind}:</strong> ${err.message ?? ""}</div>
    ${needsAuth ? `<button class="primary" id="go-options">Open options</button>` : ""}
  `;
  if (needsAuth) {
    document.getElementById("go-options").addEventListener("click", () => chrome.runtime.openOptionsPage());
  }
}

function pinBanner(data) {
  // Shown above the role when the match came from a tab pin (i.e. the
  // current URL didn't match a tracked job, but a previous page on this tab
  // did). Lets the user unpin if the auto-pin grabbed the wrong job.
  if (!data.pinned) return "";
  return `
    <div class="pin-banner">
      <span>📌 Pinned from a previous page on this tab</span>
      <button class="link" id="unpin">✕ unpin</button>
    </div>`;
}

function bindUnpin() {
  const btn = document.getElementById("unpin");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    setStatus("Unpinning…");
    await send("unpin-tab");
    setStatus("Unpinned.", "success");
    render();
  });
}

async function renderMatch(data, tab) {
  if (!data.job_id) {
    root.innerHTML = `
      <p class="note">No matching job in your tracker yet.</p>
      <button class="primary" id="track">Add this URL to tracker</button>
    `;
    document.getElementById("track").addEventListener("click", async () => {
      setStatus("Adding…");
      const r = await send("track-url", {
        url: tab.url,
        page_title: tab.title,
        company: guessCompany(tab.title, new URL(tab.url).hostname),
      });
      if (!r?.ok) { setStatus(r?.error?.message || "Failed", "fail"); return; }
      setStatus("Added — re-opening to refresh.", "success");
      render();
    });
    return;
  }

  const score = data.match_score;
  const chips = (data.keywords_targeted || []).slice(0, 6)
    .map((k) => `<span class="chip">${escape(k)}</span>`).join("");

  if (data.tailored_resume_available) {
    root.innerHTML = `
      ${pinBanner(data)}
      <div class="role">
        <div style="flex:1; min-width:0;">
          <h2>${escape(data.title || "")}</h2>
          <div class="company">${escape(data.company || "")}</div>
        </div>
        <div class="pill ${scoreClass(score)}">${fmtScore(score)}</div>
      </div>
      <div class="chips">${chips}</div>
      <div class="btn-stack">
        <button class="primary" id="auto-apply">🤖 Auto-apply (multi-page)</button>
        <button id="attach-and-autofill">Attach + autofill (this page only)</button>
        <button id="autofill-only">Autofill only</button>
        <button id="attach-only">Attach resume only</button>
      </div>
      <p class="note" style="margin-top:8px;">
        Auto-apply fills every page and clicks Continue. Stops at Submit so you can review — never auto-submits.
      </p>
    `;
    bindUnpin();
    bindFillButtons(tab.id, data.job_id);
    return;
  }

  // Matched but no tailored resume yet.
  root.innerHTML = `
    ${pinBanner(data)}
    <div class="role">
      <div style="flex:1; min-width:0;">
        <h2>${escape(data.title || "")}</h2>
        <div class="company">${escape(data.company || "")}</div>
      </div>
      <div class="pill ${scoreClass(score)}">${fmtScore(score)}</div>
    </div>
    <p class="note">${score == null ? "Not scored yet." : "Tailored resume not ready yet."}</p>
    <button class="primary" id="open-job">Open in FindMeMyJob</button>
  `;
  bindUnpin();
  document.getElementById("open-job").addEventListener("click", async () => {
    const r = await send("get-backend-url");
    const base = r?.data?.backend_url || "http://localhost:8000";
    chrome.tabs.create({ url: `${base.replace(/\/+$/, "")}/jobs/${data.job_id}` });
  });
}

function bindFillButtons(tabId, jobId) {
  const wire = (id, kind, label) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", async () => {
      setStatus(`${label}…`);
      const resp = await tabSend(tabId, { kind, job_id: jobId });
      if (!resp) {
        setStatus("No response — is this a supported ATS page? Reload the tab.", "fail");
        return;
      }
      if (!resp.ok) {
        setStatus(resp.error?.message || "Failed", "fail");
        return;
      }
      const data = resp.data || {};
      const filled = (data.filled || []).length;
      const skipped = (data.skipped || []).length;
      const attached = data.attached ? "resume attached, " : "";
      // auto-apply returns extra fields (pages, reason) — surface them.
      const pageInfo = data.pages ? ` across ${data.pages} page${data.pages === 1 ? "" : "s"}` : "";
      const reasonInfo = data.reason && data.reason !== "stopped_at_submit" && data.reason !== "submitted"
        ? ` (${data.reason.replaceAll("_", " ")})` : "";
      setStatus(
        `${attached}${filled} field${filled === 1 ? "" : "s"} filled${pageInfo}, ${skipped} skipped${reasonInfo}.`,
        filled || data.attached ? "success" : "fail"
      );
    });
  };
  wire("auto-apply", "auto-apply", "Auto-applying");
  wire("attach-and-autofill", "attach-and-autofill", "Attaching + filling");
  wire("autofill-only", "autofill-only", "Filling");
  wire("attach-only", "attach-resume-only", "Attaching");
}

function escape(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

render();
