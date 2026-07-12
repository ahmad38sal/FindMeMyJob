// Settings-aware fetch client. Used from background (service worker, ES module)
// and from the options/popup pages (ES module via <script type="module">).
//
// Why typed errors: the popup needs to distinguish "you haven't set up the
// token yet" (→ open options) from "the backend is down" (→ retry) from
// "this job isn't tailored yet" (→ open in FindMeMyJob).

export class ConfigError extends Error { constructor(m) { super(m); this.kind = "ConfigError"; } }
export class AuthError   extends Error { constructor(m) { super(m); this.kind = "AuthError"; } }
export class NotFoundError extends Error { constructor(m, body) { super(m); this.kind = "NotFoundError"; this.body = body; } }
export class NetworkError extends Error { constructor(m) { super(m); this.kind = "NetworkError"; } }
export class ApiError    extends Error { constructor(m, status, body) { super(m); this.kind = "ApiError"; this.status = status; this.body = body; } }

const DEFAULTS = { backend_url: "http://localhost:8000", token: "" };

export async function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(DEFAULTS, (items) => resolve(items));
  });
}

export async function setSettings(partial) {
  return new Promise((resolve) => {
    chrome.storage.sync.set(partial, () => resolve());
  });
}

async function apiFetch(path, init = {}) {
  const { backend_url, token } = await getSettings();
  if (!backend_url || !token) {
    throw new ConfigError("Backend URL or token not set — open the extension Options page.");
  }
  const url = backend_url.replace(/\/+$/, "") + "/api/ext" + path;
  let res;
  try {
    res = await fetch(url, {
      ...init,
      headers: {
        "Authorization": `Bearer ${token}`,
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(init.headers || {}),
      },
    });
  } catch (e) {
    throw new NetworkError(`Could not reach ${backend_url}: ${e.message}`);
  }

  if (res.status === 401) throw new AuthError("Bad or missing token — re-check Options.");
  if (res.status === 404) {
    let body = null;
    try { body = await res.json(); } catch (_) {}
    throw new NotFoundError("Not found", body);
  }
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch (_) {}
    throw new ApiError(`API ${res.status}`, res.status, body);
  }
  return res;
}

export async function health() {
  const res = await apiFetch("/health");
  return res.json();
}

export async function matchByUrl({ url, page_title, company, job_id }) {
  const res = await apiFetch("/match-by-url", {
    method: "POST",
    body: JSON.stringify({ url, page_title, company, job_id }),
  });
  return res.json();
}

export async function trackUrl({ url, page_title, company }) {
  const res = await apiFetch("/track-url", {
    method: "POST",
    body: JSON.stringify({ url, page_title, company }),
  });
  return res.json();
}

export async function getAutofillPayload(jobId) {
  const res = await apiFetch(`/jobs/${jobId}/autofill-payload`);
  return res.json();
}

// Normalized, ATS-safe application data (dates as MMM YYYY + month/year parts +
// current bool). Superset of the autofill payload — preferred by the Workday
// adapter so messy resume dates fill Workday's split date selects correctly.
export async function getApplicationData(jobId) {
  const res = await apiFetch(`/application-data/${jobId}`);
  return res.json();
}

// Returns the resume as a transferable envelope: blobs can't cross
// runtime.sendMessage, so callers reconstruct the Blob on their side.
export async function getTailoredResumeEnvelope(jobId) {
  const res = await apiFetch(`/jobs/${jobId}/tailored-resume.pdf`);
  const buf = await res.arrayBuffer();
  // Filename comes from Content-Disposition; fall back if the header is missing.
  const cd = res.headers.get("Content-Disposition") || "";
  const m = /filename="?([^"]+)"?/i.exec(cd);
  const filename = m ? m[1] : `resume-job-${jobId}.pdf`;
  return {
    bytes: Array.from(new Uint8Array(buf)),
    mimeType: res.headers.get("Content-Type") || "application/pdf",
    filename,
  };
}

export async function getBackendUrl() {
  const { backend_url } = await getSettings();
  return backend_url;
}

export async function llmFillSuggest(body) {
  const res = await apiFetch("/llm-fill-suggest", {
    method: "POST",
    body: JSON.stringify(body),
  });
  return res.json();
}
