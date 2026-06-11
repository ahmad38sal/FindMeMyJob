// Autofill engine. Two paths, run in order:
//
//   1. Heuristic adapter pass — adapter declares fieldMap (single fields)
//      and/or sections (repeating rows). Free, fast, used for the easy stuff
//      (Greenhouse name/email). Most adapters get a small but meaningful
//      head-start out of this.
//
//   2. LLM smart-fill pass — snapshot every visible interactable element +
//      labels + automation-ids, ship to the backend, get back a list of
//      {element_id, value} fills + any "click Add Another N times" actions
//      for repeating sections. Up to 2 LLM passes per click (first to expand
//      rows, second to fill them). Bounds cost.
//
// The two passes share a writeValue() helper so React-controlled inputs get
// the native-setter trick applied uniformly.

const NATIVE_VALUE_SET = {
  input: Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set,
  textarea: Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")?.set,
  select: Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value")?.set,
};

const STYLE_ID = "findmemyjob-style";
function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const s = document.createElement("style");
  s.id = STYLE_ID;
  s.textContent = `
    .findmemyjob-filled {
      outline: 2px solid #0a84ff !important;
      outline-offset: 1px !important;
      transition: outline-color 1.2s ease-out !important;
    }
    .findmemyjob-fading { outline-color: transparent !important; }
  `;
  document.documentElement.appendChild(s);
}

export function flash(el) {
  if (!el || !el.classList) return;
  injectStyle();
  el.classList.add("findmemyjob-filled");
  setTimeout(() => el.classList.add("findmemyjob-fading"), 50);
  setTimeout(() => {
    el.classList.remove("findmemyjob-filled");
    el.classList.remove("findmemyjob-fading");
  }, 1500);
}

function dispatch(el, type) {
  el.dispatchEvent(new Event(type, { bubbles: true, composed: true }));
}

// React tracks input values via its own internal state. el.value="x" updates
// the DOM but React reverts on next render. Calling the prototype setter is
// what React itself does internally, so React picks the change up.
function setReactValue(el, value) {
  const tag = el.tagName.toLowerCase();
  const setter = NATIVE_VALUE_SET[tag];
  if (setter) setter.call(el, value);
  else el.value = value;
  dispatch(el, "input");
  dispatch(el, "change");
}

function fillSelect(sel, value) {
  const target = String(value ?? "").trim().toLowerCase();
  if (!target) return false;
  let match = null;
  for (const opt of sel.options) if ((opt.value || "").toLowerCase() === target) { match = opt; break; }
  if (!match) for (const opt of sel.options) if ((opt.textContent || "").trim().toLowerCase() === target) { match = opt; break; }
  if (!match) for (const opt of sel.options) if ((opt.textContent || "").trim().toLowerCase().includes(target)) { match = opt; break; }
  if (!match) return false;
  setReactValue(sel, match.value);
  return true;
}

function fillCheckable(el, value) {
  const wantOn = !!value && value !== "false" && value !== "no";
  if (el.checked !== wantOn) el.click();
  return true;
}

function isComboboxLike(el) {
  if (!el) return false;
  const role = (el.getAttribute("role") || "").toLowerCase();
  if (role === "combobox" || role === "listbox") return true;
  if (el.getAttribute("aria-haspopup") === "listbox") return true;
  if (el.getAttribute("aria-autocomplete")) return true;
  // Workday-specific: divs/buttons sitting inside a multi-select container.
  if (el.closest && el.closest('[data-automation-id="multiSelectContainer"]')) return true;
  if ((el.getAttribute("data-automation-id") || "").toLowerCase().includes("multiselect")) return true;
  return false;
}

async function findMatchingOption(value, timeoutMs = 1500) {
  const target = String(value).trim().toLowerCase();
  if (!target) return null;
  const start = Date.now();
  let lastSeen = 0;
  while (Date.now() - start < timeoutMs) {
    const options = document.querySelectorAll(
      '[role="option"], [data-automation-id="promptOption"], li[role="presentation"] [role="option"]'
    );
    if (options.length || lastSeen) {
      for (const opt of options) {
        const t = (opt.textContent || "").trim().toLowerCase();
        if (t === target) return opt;
      }
      for (const opt of options) {
        const t = (opt.textContent || "").trim().toLowerCase();
        if (t.includes(target)) return opt;
      }
    }
    lastSeen = options.length;
    await sleep(80);
  }
  return null;
}

// Workday multi-select / role=combobox / aria-haspopup=listbox: click to open,
// type into any search input that appears, click the matching option.
async function writeCombobox(el, value) {
  // If the element itself is the trigger (button/div), click it. If it's the
  // text input *inside* a typeahead, set its value to filter.
  const tag = el.tagName.toLowerCase();
  const isTextInput = tag === "input" && (el.type === "text" || !el.type);

  // Open by clicking either the element or the wrapping multiselect container.
  const opener = isTextInput
    ? el
    : (el.closest('[data-automation-id="multiSelectContainer"]') || el);
  try { opener.click(); } catch (_) {}
  await sleep(150);

  // If a search input materialized (Workday's prompt typeahead), type into it.
  const searchInput = document.querySelector(
    '[role="combobox"][type="text"]:not([disabled]), input[aria-autocomplete="list"]:not([disabled]), [data-automation-id="searchBox"]:not([disabled])'
  ) || (isTextInput ? el : null);
  if (searchInput) {
    setReactValue(searchInput, String(value));
    await sleep(280); // wait for option list to filter
  }

  const opt = await findMatchingOption(value, 1500);
  if (!opt) {
    // Close the dropdown so we don't leave the page in a weird state.
    document.body.click();
    return { ok: false, reason: `dropdown opened but no option matched "${value}"` };
  }
  opt.click();
  await sleep(120);
  // For some multi-selects, focus stays on the search input so the user can
  // type the next item — clear it for the engine's next call.
  if (searchInput && searchInput !== el) {
    try { setReactValue(searchInput, ""); } catch (_) {}
  }
  return { ok: true };
}

function resolveFinder(finder, key, root) {
  if (typeof finder === "function") return finder(root, key);
  if (typeof finder === "string") return root.querySelector(finder);
  if (finder && typeof finder === "object" && finder.selector) return root.querySelector(finder.selector);
  return null;
}

// Single-element write. Returns {ok:true} or {ok:false, reason}.
async function writeValue(el, value, customControl, root) {
  if (customControl) {
    try {
      const handled = await customControl(el, value, root);
      if (handled) { flash(el); return { ok: true }; }
      return { ok: false, reason: "custom control did not handle value" };
    } catch (e) { return { ok: false, reason: `custom control threw: ${e.message}` }; }
  }
  // Multi-select / multi-value combobox: caller passed an array → fill each in turn.
  if (Array.isArray(value)) {
    if (!isComboboxLike(el)) {
      // Not a combobox — fall back to comma-joined string.
      return writeValue(el, value.join(", "), null, root);
    }
    let firstOk = false;
    let lastReason = "";
    for (const item of value) {
      const r = await writeCombobox(el, item);
      if (r.ok) firstOk = true;
      else lastReason = r.reason;
    }
    if (firstOk) { flash(el); return { ok: true }; }
    return { ok: false, reason: lastReason || "no values selected" };
  }
  // Single combobox / typeahead.
  if (isComboboxLike(el)) {
    const r = await writeCombobox(el, value);
    if (r.ok) flash(el);
    return r;
  }
  const tag = el.tagName.toLowerCase();
  const type = (el.type || "").toLowerCase();
  try {
    if (tag === "select") {
      if (fillSelect(el, value)) { flash(el); return { ok: true }; }
      return { ok: false, reason: `no <option> matched "${value}"` };
    }
    if (type === "checkbox" || type === "radio") {
      fillCheckable(el, value);
      flash(el);
      return { ok: true };
    }
    if (el.isContentEditable) {
      el.focus();
      document.execCommand("insertText", false, String(value));
      el.blur();
      flash(el);
      return { ok: true };
    }
    setReactValue(el, String(value));
    flash(el);
    return { ok: true };
  } catch (e) { return { ok: false, reason: `write failed: ${e.message}` }; }
}

// Walks adapter.fieldMap, applies values from payload, returns {filled, skipped}.
export async function runAutofill(payload, adapter, root = document) {
  const filled = [];
  const skipped = [];
  const map = adapter.fieldMap || {};
  for (const [key, finder] of Object.entries(map)) {
    const value = payload[key];
    if (value === undefined || value === null || value === "") {
      skipped.push({ key, reason: "no value in payload" });
      continue;
    }
    let el;
    try { el = resolveFinder(finder, key, root); }
    catch (e) { skipped.push({ key, reason: `finder threw: ${e.message}` }); continue; }
    if (!el) { skipped.push({ key, reason: "input not found on page" }); continue; }
    const r = await writeValue(el, value, adapter.customControls?.[key], root);
    if (r.ok) filled.push(key);
    else skipped.push({ key, reason: r.reason });
  }
  if (typeof adapter.afterFill === "function") {
    try { await adapter.afterFill(root, { filled, skipped }); } catch (_) {}
  }
  return { filled, skipped };
}

// Walks adapter.sections (repeating lists). See workday adapter for example.
export async function runSections(payload, adapter, root = document) {
  const filled = [];
  const skipped = [];
  if (!Array.isArray(adapter.sections)) return { filled, skipped };
  for (const section of adapter.sections) {
    const items = Array.isArray(payload[section.payloadKey]) ? payload[section.payloadKey] : [];
    if (!items.length) continue;
    const cap = Math.min(items.length, section.maxRows ?? 10);
    const wantedItems = items.slice(0, cap);
    let rows = [];
    try { rows = section.detect(root) || []; }
    catch (e) { skipped.push({ key: `${section.name}._detect`, reason: e.message }); continue; }
    let attempts = 0;
    while (rows.length < wantedItems.length && attempts < cap + 2) {
      attempts++;
      let newRow = null;
      try { newRow = await section.addRow(root); }
      catch (e) { skipped.push({ key: `${section.name}._add[${attempts}]`, reason: e.message }); break; }
      if (!newRow) { skipped.push({ key: `${section.name}._add[${attempts}]`, reason: "addRow returned null" }); break; }
      await sleep(120);
      try { rows = section.detect(root) || []; } catch { /* keep prior */ }
    }
    for (let i = 0; i < wantedItems.length; i++) {
      const row = rows[i];
      if (!row) { skipped.push({ key: `${section.name}[${i}]`, reason: "row not present" }); continue; }
      let rowValues;
      try { rowValues = section.perRowMap(wantedItems[i]); }
      catch (e) { skipped.push({ key: `${section.name}[${i}]._map`, reason: e.message }); continue; }
      for (const [key, finder] of Object.entries(section.perRowFieldMap || {})) {
        const value = rowValues[key];
        if (value === undefined || value === null || value === "") {
          skipped.push({ key: `${section.name}[${i}].${key}`, reason: "no value" });
          continue;
        }
        let el;
        try { el = resolveFinder(finder, key, row); }
        catch (e) { skipped.push({ key: `${section.name}[${i}].${key}`, reason: `finder threw: ${e.message}` }); continue; }
        if (!el) { skipped.push({ key: `${section.name}[${i}].${key}`, reason: "input not found in row" }); continue; }
        const r = await writeValue(el, value, section.customControls?.[key], row);
        if (r.ok) filled.push(`${section.name}[${i}].${key}`);
        else skipped.push({ key: `${section.name}[${i}].${key}`, reason: r.reason });
      }
    }
  }
  return { filled, skipped };
}

// ----- Page snapshot for LLM -----

function isVisible(el) {
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return false;
  const cs = getComputedStyle(el);
  if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return false;
  return true;
}

function getEffectiveLabel(el) {
  if (el.id) {
    try {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return (lab.textContent || "").trim();
    } catch {}
  }
  const wrap = el.closest && el.closest("label");
  if (wrap) return (wrap.textContent || "").trim();
  // Workday: the formField-{name} ancestor div has the visible label as its
  // first child or as a child element with class containing "label".
  const ff = el.closest && el.closest('[data-automation-id^="formField-"]');
  if (ff) {
    const lab = ff.querySelector("label, [class*='label' i]");
    if (lab) return (lab.textContent || "").trim();
    const t = (ff.textContent || "").trim();
    return t.split("\n").map((s) => s.trim()).find(Boolean) || "";
  }
  return "";
}

function findAncestorAutomationId(el) {
  let cur = el.parentElement;
  while (cur && cur !== document.body) {
    const aid = cur.getAttribute && cur.getAttribute("data-automation-id");
    if (aid && aid !== el.getAttribute("data-automation-id")) return aid;
    cur = cur.parentElement;
  }
  return "";
}

function findAncestorRowSiblings(el) {
  // For repeating sections — the LLM benefits from knowing how many siblings
  // share this element's structure (e.g. "this is row 0 of 1 work-experience
  // rows currently visible"). Cheapest signal: count formField siblings.
  const ff = el.closest && el.closest('[data-automation-id^="formField-"]');
  if (!ff || !ff.parentElement) return null;
  const id = ff.getAttribute("data-automation-id");
  return ff.parentElement.querySelectorAll(`[data-automation-id="${id}"]`).length;
}

// Build a flat schema of every interactable visible element on the page.
// Returns { elements, registry, headings }. Registry is element_id → DOM node.
export function snapshotPage() {
  // Standard interactable tags + Workday's prompt widgets, which are divs
  // dressed up as comboboxes (no real <input> until clicked).
  const candidates = document.querySelectorAll(
    'input, textarea, select, button,' +
    ' [role="combobox"], [role="listbox"], [role="textbox"],' +
    ' [contenteditable="true"], [aria-haspopup="listbox"],' +
    ' [data-automation-id="multiSelectContainer"]'
  );
  const seen = new Set();
  const elements = [];
  const registry = {};
  let i = 0;
  for (const el of candidates) {
    if (seen.has(el)) continue;
    seen.add(el);
    if (!isVisible(el)) continue;
    if (el.disabled) continue;
    if ((el.type || "").toLowerCase() === "hidden") continue;
    const id = `el_${i++}`;
    registry[id] = el;
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    const type = (el.type || role || "").toLowerCase();
    const aid = el.getAttribute("data-automation-id") || "";
    const isCombo = isComboboxLike(el);
    elements.push({
      element_id: id,
      tag,
      type: isCombo ? `combobox${role ? `:${role}` : ""}` : type,
      name: (el.getAttribute("name") || "").slice(0, 60),
      id_attr: (el.id || "").slice(0, 60),
      automation_id: aid,
      label: getEffectiveLabel(el).slice(0, 120),
      aria_label: (el.getAttribute("aria-label") || "").slice(0, 100),
      aria_haspopup: el.getAttribute("aria-haspopup") || "",
      aria_multiselectable: el.getAttribute("aria-multiselectable") || "",
      placeholder: (el.getAttribute("placeholder") || "").slice(0, 80),
      button_text: tag === "button" ? (el.textContent || "").trim().slice(0, 60) : "",
      container_automation_id: findAncestorAutomationId(el),
      sibling_count: findAncestorRowSiblings(el),
      checked: type === "checkbox" || type === "radio" ? !!el.checked : undefined,
    });
  }
  const headings = Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"], [data-automation-id="instructionalText"]'))
    .map((h) => (h.textContent || "").trim())
    .filter(Boolean)
    .slice(0, 25);
  return { elements, registry, headings };
}

// Smart-fill loop (heuristic + LLM passes). All steps log to the page
// console under a "FindMeMyJob" group so failures are visible without
// needing to instrument the popup.

async function smartFill(payload, adapter) {
  const out = { filled: [], skipped: [], attached: false };
  const log = (...args) => console.log("[FindMeMyJob]", ...args);
  console.group("[FindMeMyJob] smartFill");
  log("adapter:", adapter.name, "url:", location.href);
  log("payload keys:", Object.keys(payload));

  // Pass 0: heuristic adapter (free, fast).
  if (adapter.fieldMap) {
    const auto = await runAutofill(payload, adapter);
    log(`heuristic fieldMap: filled=${auto.filled.length} skipped=${auto.skipped.length}`,
        { filled: auto.filled, skipped: auto.skipped });
    out.filled.push(...auto.filled);
    out.skipped.push(...auto.skipped);
  }
  if (Array.isArray(adapter.sections)) {
    const sec = await runSections(payload, adapter);
    log(`heuristic sections: filled=${sec.filled.length} skipped=${sec.skipped.length}`,
        { filled: sec.filled, skipped: sec.skipped });
    out.filled.push(...sec.filled);
    out.skipped.push(...sec.skipped);
  }

  // Pass 1+: LLM smart fill loop. Up to 2 passes — first may expand rows,
  // second fills them.
  for (let pass = 1; pass <= 2; pass++) {
    const snap = snapshotPage();
    log(`pass ${pass}: snapshot — ${snap.elements.length} elements, ${snap.headings.length} headings`);
    if (!snap.elements.length) { log("no elements visible — bailing"); break; }

    const t0 = performance.now();
    const r = await sendBg("llm-fill-suggest", {
      page_url: location.href,
      page_title: document.title,
      headings: snap.headings,
      elements: snap.elements,
      payload,
      already_filled_keys: out.filled.map((k) => String(k).split(".")[0]),
      pass_number: pass,
    });
    log(`pass ${pass}: LLM call took ${Math.round(performance.now() - t0)}ms`, r);
    if (!r?.ok) {
      // Spell the error out — Chrome's console truncates raw objects to "Object".
      const err = r?.error || {};
      console.error("[FindMeMyJob] LLM call failed:",
        "kind=", err.kind || "(none)",
        "status=", err.status || "(none)",
        "message=", err.message || "(none)",
        "body=", err.body || "(none)");
      out.skipped.push({ key: "_llm", reason: `${err.kind || "Error"}: ${err.message || "LLM call failed"}` });
      break;
    }
    const result = r.data || {};
    const fills = Array.isArray(result.fills) ? result.fills : [];
    const clicks = Array.isArray(result.clicks) ? result.clicks : [];
    log(`pass ${pass}: LLM returned ${fills.length} fills, ${clicks.length} clicks, needs_resnapshot=${result.needs_resnapshot}`);
    if (result.note) log(`pass ${pass}: LLM note —`, result.note);

    for (const fill of fills) {
      const el = snap.registry[fill.element_id];
      if (!el) {
        log(`  fill ${fill.element_id} (${fill.canonical_key || "?"}): el missing from registry`);
        out.skipped.push({ key: fill.canonical_key || fill.element_id, reason: "el missing from registry" });
        continue;
      }
      const w = await writeValue(el, fill.value);
      if (w.ok) {
        log(`  ✓ fill ${fill.element_id} (${fill.canonical_key || "?"}) ←`, fill.value);
        out.filled.push(fill.canonical_key || fill.element_id);
      } else {
        log(`  ✗ fill ${fill.element_id} (${fill.canonical_key || "?"}): ${w.reason}`);
        out.skipped.push({ key: fill.canonical_key || fill.element_id, reason: w.reason });
      }
    }

    for (const click of clicks) {
      const el = snap.registry[click.element_id];
      if (!el) { log(`  click ${click.element_id}: el missing from registry`); continue; }
      const times = Math.max(1, Math.min(20, click.times || 1));
      log(`  → clicking ${click.element_id} ×${times} (${click.purpose || ""})`);
      for (let n = 0; n < times; n++) {
        try { el.click(); } catch (e) { log(`     click threw: ${e.message}`); }
        await sleep(450);
      }
    }

    if (!result.needs_resnapshot && clicks.length === 0) { log(`pass ${pass}: nothing more to do`); break; }
    if (pass === 2) { log(`pass 2 done — exiting loop`); break; }
  }

  log(`smartFill done: total filled=${out.filled.length}, skipped=${out.skipped.length}`);
  console.groupEnd();
  return out;
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ----- Auto-advance bot loop (autonomous multi-page) -----
//
// Fills the page, clicks "Save and Continue" / "Next", waits for the next
// page to render, fills again, repeats. Stops when we reach a page that
// has a Submit button but no Continue button — by default, stops there for
// the user to review. Set opts.autoSubmit=true to also click submit (ToS
// risk on external sites — Apple internal only per project policy).

const ADVANCE_TEXT_RE = /^(save and continue|continue|next|review|save & continue)$/i;
const SUBMIT_TEXT_RE = /(submit application|submit my application|^submit$)/i;

function _isVisibleEl(el) {
  if (!el) return false;
  if (el.disabled) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return false;
  const cs = getComputedStyle(el);
  return cs.display !== "none" && cs.visibility !== "hidden" && cs.opacity !== "0";
}

function _buttonText(el) {
  return ((el.textContent || "") + " " + (el.getAttribute("aria-label") || "")).trim();
}

function findAdvanceButton() {
  // Workday's stable selector first.
  const wd = document.querySelector('[data-automation-id="pageFooterNextButton"]');
  if (wd && _isVisibleEl(wd)) return wd;
  // Generic: any visible button whose text matches the advance vocabulary.
  const buttons = document.querySelectorAll('button, [role="button"], a[role="button"], input[type="submit"]');
  for (const b of buttons) {
    if (!_isVisibleEl(b)) continue;
    const t = _buttonText(b);
    if (SUBMIT_TEXT_RE.test(t)) continue; // never advance via Submit
    if (ADVANCE_TEXT_RE.test(t)) return b;
  }
  return null;
}

function findSubmitButton() {
  const buttons = document.querySelectorAll('button, [role="button"], input[type="submit"]');
  for (const b of buttons) {
    if (!_isVisibleEl(b)) continue;
    if (SUBMIT_TEXT_RE.test(_buttonText(b))) return b;
  }
  return null;
}

function pageFingerprint() {
  // Cheap signature of the current page for change detection. We watch
  // this both for SPA transitions (Workday's React swaps the page block
  // without a real navigation) and full reloads.
  const ids = Array.from(document.querySelectorAll("[data-automation-id]"))
    .slice(0, 80)
    .map((e) => e.getAttribute("data-automation-id"))
    .join("|");
  const inputs = document.querySelectorAll("input, textarea, select").length;
  return `${location.pathname}#${location.hash}::${inputs}::${ids.length}`;
}

async function waitForPageChange(prevSig, timeoutMs = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await sleep(250);
    if (pageFingerprint() !== prevSig) {
      // Let React finish its render before the next snapshot.
      await sleep(700);
      return true;
    }
  }
  return false;
}

function injectStatusBanner() {
  const id = "findmemyjob-auto-banner";
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement("div");
  el.id = id;
  el.style.cssText = `
    position: fixed; top: 12px; right: 12px; z-index: 2147483647;
    background: #0a84ff; color: white; font: 13px -apple-system, sans-serif;
    padding: 10px 14px; border-radius: 8px; box-shadow: 0 4px 18px rgba(0,0,0,0.25);
    max-width: 280px; line-height: 1.4;
  `;
  document.documentElement.appendChild(el);
  return el;
}

function setBanner(text, kind = "info") {
  const el = injectStatusBanner();
  el.textContent = text;
  if (kind === "ok") el.style.background = "#1d8a3a";
  else if (kind === "warn") el.style.background = "#b48a00";
  else if (kind === "error") el.style.background = "#c62828";
  else el.style.background = "#0a84ff";
}

function clearBanner() {
  const el = document.getElementById("findmemyjob-auto-banner");
  if (el) el.remove();
}

async function autoApplyLoop(jobId, adapter, upload, opts = {}) {
  const maxPages = Math.max(1, Math.min(12, opts.maxPages ?? 8));
  const autoSubmit = !!opts.autoSubmit;
  const pageDelay = Math.max(0, opts.pageDelayMs ?? 1500);
  const log = (...a) => console.log("[FindMeMyJob:auto]", ...a);

  log(`starting — jobId=${jobId} maxPages=${maxPages} autoSubmit=${autoSubmit}`);
  setBanner(`Auto-apply starting…`);

  const allFilled = [];
  const allSkipped = [];
  let attached = false;

  // Fetch payload once — same job across all pages.
  const payloadResp = await sendBg("fetch-autofill-payload", { job_id: jobId });
  if (!payloadResp?.ok) {
    setBanner(`Auto-apply failed: ${payloadResp?.error?.message || "couldn't fetch payload"}`, "error");
    return { ok: false, reason: "no_payload", filled: [], skipped: [], attached: false };
  }
  const payload = payloadResp.data;

  // Resume envelope: also fetch once. attachResume re-runs per page in case
  // a later page also has a file input (some Workday tenants ask twice).
  const resumeResp = await sendBg("fetch-tailored-resume", { job_id: jobId });
  const resumeEnvelope = resumeResp?.ok ? resumeResp.data : null;

  for (let page = 1; page <= maxPages; page++) {
    log(`page ${page}: filling`);
    setBanner(`Page ${page}: filling…`);

    // Resume attach (if a file input exists on this page and we haven't
    // already uploaded successfully — Workday confirms the upload by
    // showing the filename, so re-uploading is a no-op semantically).
    if (resumeEnvelope) {
      const r = await upload.attachResume(resumeEnvelope, adapter);
      if (r.ok) attached = true;
    }

    const fill = await smartFill(payload, adapter);
    allFilled.push(...fill.filled);
    allSkipped.push(...fill.skipped);
    log(`page ${page}: filled=${fill.filled.length} skipped=${fill.skipped.length}`);

    // Did we land on the submit page?
    const submitBtn = findSubmitButton();
    const advanceBtn = findAdvanceButton();
    const onSubmitPage = !!submitBtn && !advanceBtn;

    if (onSubmitPage) {
      log("reached submit page");
      if (autoSubmit) {
        setBanner(`Page ${page}: auto-submitting…`, "warn");
        await sleep(pageDelay);
        try { submitBtn.click(); } catch (e) { log("submit click threw:", e.message); }
        setBanner(`Auto-applied across ${page} pages — submitted.`, "ok");
        return { ok: true, reason: "submitted", pages: page, filled: allFilled, skipped: allSkipped, attached };
      }
      setBanner(`Reached Submit page — review then submit yourself (${allFilled.length} fields filled across ${page} pages)`, "ok");
      return { ok: true, reason: "stopped_at_submit", pages: page, filled: allFilled, skipped: allSkipped, attached };
    }

    if (!advanceBtn) {
      log("no advance button found on page", page);
      setBanner(`Page ${page}: no Continue button found — stopping. Filled ${allFilled.length} fields.`, "warn");
      return { ok: false, reason: "no_advance_button", pages: page, filled: allFilled, skipped: allSkipped, attached };
    }

    // Hand-off pause so the user can see what got filled before we click.
    setBanner(`Page ${page} filled (${fill.filled.length} fields). Continuing in ${Math.round(pageDelay / 1000)}s…`);
    await sleep(pageDelay);

    const beforeSig = pageFingerprint();
    log(`clicking advance: "${_buttonText(advanceBtn).slice(0, 40)}"`);
    try { advanceBtn.click(); } catch (e) { log("advance click threw:", e.message); }

    setBanner(`Page ${page}: waiting for next page…`);
    const navigated = await waitForPageChange(beforeSig, 10000);
    if (!navigated) {
      log("page didn't change after advance click");
      setBanner(`Page ${page} didn't advance — likely a validation error. Check the page and re-run.`, "error");
      return { ok: false, reason: "page_didnt_advance", pages: page, filled: allFilled, skipped: allSkipped, attached };
    }
  }

  setBanner(`Hit page limit (${maxPages}). Stopping. Filled ${allFilled.length} fields total.`, "warn");
  return { ok: false, reason: "max_pages_exceeded", pages: maxPages, filled: allFilled, skipped: allSkipped, attached };
}

// Wires content-script message handling. Each adapter's loader calls this
// with its adapter object after dynamic-importing engine + upload.
export function registerMessageListener(adapter, upload) {
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || !msg.kind) return false;
    const supported = ["attach-and-autofill", "autofill-only", "attach-resume-only", "auto-apply"];
    if (!supported.includes(msg.kind)) return false;
    (async () => {
      try {
        if (msg.kind === "auto-apply") {
          const result = await autoApplyLoop(msg.job_id, adapter, upload, {
            autoSubmit: !!msg.auto_submit,
            maxPages: msg.max_pages,
            pageDelayMs: msg.page_delay_ms,
          });
          sendResponse({ ok: true, data: result });
          return;
        }
        const out = { filled: [], skipped: [], attached: false };
        let payload = null;
        if (msg.kind !== "attach-resume-only") {
          const r = await sendBg("fetch-autofill-payload", { job_id: msg.job_id });
          if (!r?.ok) throw makeErr(r?.error);
          payload = r.data;
        }
        if (msg.kind !== "autofill-only") {
          const r = await sendBg("fetch-tailored-resume", { job_id: msg.job_id });
          if (!r?.ok) throw makeErr(r?.error);
          const result = await upload.attachResume(r.data, adapter);
          out.attached = result.ok;
          if (!result.ok) out.skipped.push({ key: "_resume", reason: result.reason });
        }
        if (payload) {
          const fill = await smartFill(payload, adapter);
          out.filled.push(...fill.filled);
          out.skipped.push(...fill.skipped);
        }
        sendResponse({ ok: true, data: out });
      } catch (e) {
        sendResponse({ ok: false, error: { kind: e.kind || "Error", message: e.message } });
      }
    })();
    return true;
  });
}

function sendBg(kind, data) {
  return new Promise((resolve) => chrome.runtime.sendMessage({ kind, ...data }, resolve));
}

function makeErr(err) {
  const e = new Error(err?.message || "request failed");
  e.kind = err?.kind || "Error";
  return e;
}
