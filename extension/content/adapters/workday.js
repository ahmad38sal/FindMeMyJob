// Workday — *.myworkdayjobs.com/...
//
// Workday tenants vary in how they name automation-ids. This adapter targets
// the most common modern scheme (verified against a real apply flow):
//
//   Field wrappers:    [data-automation-id="formField-{name}"]
//                      The actual <input>/<textarea> is nested inside.
//   Row container:     not labelled — derive it as the smallest ancestor of
//                      formField-jobTitle that also contains formField-companyName
//                      and only one formField-jobTitle.
//   Add another:       [data-automation-id="add-button"] (may appear multiple
//                      times — one per section: work, education, languages,
//                      certifications). We pick the one positioned immediately
//                      after the last work-experience row.
//   Date input:        formField-startDate / formField-endDate >
//                      dateSectionMonth-input + dateSectionYear-input (text spinners)
//   Resume upload:     file-upload-input-ref
//
// What's covered:
//   My Information page: first/last name, email, phone, address, city, postal, country, region
//   My Experience page:  work_history repeating rows (full)
//                        education repeating rows (best-effort — depends on tenant labels)
//
// Not yet covered (intentional):
//   self-identify / EEO / voluntary disclosures
//   sourceQuestion (varies per tenant — free text)
//   work-auth / sponsorship radios (high error cost)
//   the "Save and Continue" navigation
//   skills multi-select (Workday's prompt-style multiselect — separate work)

// ---- Helpers ----

const $ = (root, sel) => root.querySelector(sel);
const $$ = (root, sel) => Array.from(root.querySelectorAll(sel));

// Find the row containers for a given anchor field (e.g. "jobTitle"). Each
// row is the smallest ancestor of an anchor formField that has a sibling
// formField from the same logical row but no other instances of the anchor.
function rowsByAnchor(anchorField, witnessField) {
  const anchors = $$(document, `[data-automation-id="formField-${anchorField}"]`);
  const rows = [];
  for (const a of anchors) {
    let cur = a.parentElement;
    while (cur && cur !== document.body) {
      const anchorsInside = cur.querySelectorAll(`[data-automation-id="formField-${anchorField}"]`);
      const hasWitness = !!cur.querySelector(`[data-automation-id="formField-${witnessField}"]`);
      if (anchorsInside.length === 1 && hasWitness) { rows.push(cur); break; }
      cur = cur.parentElement;
    }
  }
  return rows;
}

// Find the add-button positioned immediately after a given row container in
// document order. When no row yet exists for a section, fall back to the Nth
// add-button on the page (Workday orders them: work > education > … ).
function findNextAddButtonAfter(node) {
  const buttons = $$(document, '[data-automation-id="add-button"]');
  for (const b of buttons) {
    if (node.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) return b;
  }
  return null;
}

function nthAddButton(n) {
  return $$(document, '[data-automation-id="add-button"]')[n] || null;
}

async function clickAndWait(button, ms = 350) {
  if (!button) return false;
  button.click();
  await new Promise((r) => setTimeout(r, ms));
  return true;
}

function waitFor(selector, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const found = document.querySelector(selector);
    if (found) return resolve(found);
    const start = Date.now();
    const obs = new MutationObserver(() => {
      const m = document.querySelector(selector);
      if (m) { obs.disconnect(); resolve(m); }
      else if (Date.now() - start > timeoutMs) { obs.disconnect(); resolve(null); }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(() => { obs.disconnect(); resolve(null); }, timeoutMs);
  });
}

// Profile dates are ISO ("2024-02-15" or "2024-02"). Workday wants MM and YYYY
// in two separate text spinners.
function parseDateParts(iso) {
  if (!iso) return { month: "", year: "" };
  const m = /^(\d{4})-(\d{2})/.exec(String(iso));
  if (!m) return { month: "", year: "" };
  return { month: m[2], year: m[1] };
}

// Date parts for a section item, preferring the normalized month/year fields
// (application-data endpoint) and falling back to parsing item.start/item.end.
// Months are returned zero-padded ("02") to match Workday's spinner values.
function normalizedParts(item, which) {
  const mo = item[`${which}_month`];
  const yr = item[`${which}_year`];
  if (mo != null || yr != null) {
    return {
      month: mo != null ? String(mo).padStart(2, "0") : "",
      year: yr != null ? String(yr) : "",
    };
  }
  return parseDateParts(item[which]);
}

// Workday <button>+listbox dropdown control: click trigger, wait for options, click match.
async function clickWorkdayDropdown(triggerEl, value) {
  if (!triggerEl) return false;
  triggerEl.click();
  await waitFor("[role='listbox'] [role='option'], [data-automation-id='promptOption']");
  const target = String(value).trim().toLowerCase();
  const options = document.querySelectorAll("[role='listbox'] [role='option'], [data-automation-id='promptOption']");
  for (const opt of options) {
    const text = (opt.textContent || "").trim().toLowerCase();
    if (text === target || text.includes(target)) { opt.click(); return true; }
  }
  // Close the popup if we couldn't find a match.
  document.body.click();
  return false;
}

// Try several selector candidates inside a root and return the first hit.
function firstMatch(root, selectors) {
  for (const sel of selectors) {
    const el = root.querySelector(sel);
    if (el) return el;
  }
  return null;
}

// ---- Adapter ----

export default {
  name: "workday",
  urlPattern: "*://*.myworkdayjobs.com/*",

  // Use the ats.py-normalized endpoint: work_history rows arrive with
  // start_month/start_year/end_month/end_year already split out (robust to
  // messy resume dates like "January 2023" or "01/2023") plus a `current`
  // bool. perRowMap below prefers those, so Workday's split date spinners +
  // "I currently work here" checkbox fill correctly regardless of the raw
  // stored format.
  payloadEndpoint: "application-data",

  // My Information page — single-row fields. We try the common formField-*
  // pattern first, falling back to older Workday selectors. The actual input
  // is usually nested inside the formField div.
  fieldMap: {
    first_name:    (root) => firstMatch(root, [
      "[data-automation-id='formField-firstName'] input",
      "[data-automation-id='formField-legalNameSection_firstName'] input",
      "[data-automation-id='legalNameSection_firstName']",
      "input[data-automation-id$='firstName']",
    ]),
    last_name:     (root) => firstMatch(root, [
      "[data-automation-id='formField-lastName'] input",
      "[data-automation-id='formField-legalNameSection_lastName'] input",
      "[data-automation-id='legalNameSection_lastName']",
      "input[data-automation-id$='lastName']",
    ]),
    email:         (root) => firstMatch(root, [
      "[data-automation-id='formField-email'] input",
      "[data-automation-id='email']",
      "input[data-automation-id$='email']",
    ]),
    phone:         (root) => firstMatch(root, [
      "[data-automation-id='formField-phone-number'] input",
      "[data-automation-id='formField-phoneNumber'] input",
      "[data-automation-id='phone-number']",
      "input[data-automation-id*='phone']",
    ]),
    address_line1: (root) => firstMatch(root, [
      "[data-automation-id='formField-addressLine1'] input",
      "[data-automation-id='formField-addressSection_addressLine1'] input",
      "[data-automation-id='addressSection_addressLine1']",
      "input[data-automation-id$='addressLine1']",
    ]),
    city:          (root) => firstMatch(root, [
      "[data-automation-id='formField-city'] input",
      "[data-automation-id='formField-addressSection_city'] input",
      "[data-automation-id='addressSection_city']",
      "input[data-automation-id$='city']",
    ]),
    postal_code:   (root) => firstMatch(root, [
      "[data-automation-id='formField-postalCode'] input",
      "[data-automation-id='formField-addressSection_postalCode'] input",
      "[data-automation-id='addressSection_postalCode']",
      "input[data-automation-id$='postalCode']",
    ]),
  },
  customControls: {
    country: async (_el, value) => {
      const trigger = firstMatch(document, [
        "[data-automation-id='formField-country'] button",
        "[data-automation-id='countryDropdown']",
        "[data-automation-id='formField-countryDropdown'] button",
      ]);
      return clickWorkdayDropdown(trigger, value);
    },
    region: async (_el, value) => {
      const trigger = firstMatch(document, [
        "[data-automation-id='formField-countryRegion'] button",
        "[data-automation-id='formField-addressSection_countryRegion'] button",
        "[data-automation-id='addressSection_countryRegion']",
      ]);
      return clickWorkdayDropdown(trigger, value);
    },
  },

  // My Experience page — repeating rows.
  sections: [
    {
      name: "work_experience",
      payloadKey: "work_history",
      maxRows: 10,
      detect: () => rowsByAnchor("jobTitle", "companyName"),
      addRow: async () => {
        const rows = rowsByAnchor("jobTitle", "companyName");
        const before = rows.length;
        const btn = before > 0
          ? findNextAddButtonAfter(rows[rows.length - 1])
          : nthAddButton(0); // first add-button is conventionally work-experience
        if (!btn) return null;
        await clickAndWait(btn, 400);
        const after = rowsByAnchor("jobTitle", "companyName");
        // Wait briefly more if the new row hasn't appeared yet (React render).
        if (after.length === before) await new Promise((r) => setTimeout(r, 300));
        const final = rowsByAnchor("jobTitle", "companyName");
        return final[final.length - 1] || null;
      },
      perRowMap: (item) => {
        // Prefer the normalized month/year parts (application-data endpoint);
        // fall back to parsing an ISO start/end (legacy autofill payload).
        const start = normalizedParts(item, "start");
        const end = normalizedParts(item, "end");
        const current = item.current != null ? !!item.current : !!item.currently_work_here;
        return {
          job_title:           item.title,
          company:             item.company,
          location:            item.location,
          start_month:         start.month,
          start_year:          start.year,
          end_month:           current ? "" : end.month,
          end_year:            current ? "" : end.year,
          currently_work_here: current,
          description:         item.description,
        };
      },
      perRowFieldMap: {
        job_title: (row) => firstMatch(row, [
          "[data-automation-id='formField-jobTitle'] input",
          "[data-automation-id='formField-jobTitle'] textarea",
        ]),
        company: (row) => firstMatch(row, [
          "[data-automation-id='formField-companyName'] input",
        ]),
        location: (row) => firstMatch(row, [
          "[data-automation-id='formField-location'] input",
          "[data-automation-id='formField-locationCountry'] input",
        ]),
        start_month: (row) => firstMatch(row, [
          "[data-automation-id='formField-startDate'] [data-automation-id='dateSectionMonth-input']",
        ]),
        start_year: (row) => firstMatch(row, [
          "[data-automation-id='formField-startDate'] [data-automation-id='dateSectionYear-input']",
        ]),
        end_month: (row) => firstMatch(row, [
          "[data-automation-id='formField-endDate'] [data-automation-id='dateSectionMonth-input']",
        ]),
        end_year: (row) => firstMatch(row, [
          "[data-automation-id='formField-endDate'] [data-automation-id='dateSectionYear-input']",
        ]),
        currently_work_here: (row) => firstMatch(row, [
          "[data-automation-id='formField-currentlyWorkHere'] input[type='checkbox']",
          "[data-automation-id='formField-currentEmployment'] input[type='checkbox']",
        ]),
        description: (row) => firstMatch(row, [
          "[data-automation-id='formField-roleDescription'] textarea",
          "[data-automation-id='formField-description'] textarea",
        ]),
      },
    },
    {
      // Education: best-effort. If your tenant doesn't have any education row
      // visible until you click "Add another", the addRow path will click the
      // 2nd add-button (Workday section order: work > education > languages >
      // certifications). If your tenant orders sections differently, the wrong
      // section may grow — verify by watching the page.
      name: "education",
      payloadKey: "education",
      maxRows: 6,
      detect: () => rowsByAnchor("school", "degree").length
        ? rowsByAnchor("school", "degree")
        : rowsByAnchor("schoolName", "degree"),
      addRow: async () => {
        const existing = (rowsByAnchor("school", "degree").length
          ? rowsByAnchor("school", "degree")
          : rowsByAnchor("schoolName", "degree"));
        const btn = existing.length > 0
          ? findNextAddButtonAfter(existing[existing.length - 1])
          : nthAddButton(1); // assume work=0, education=1 in Workday's section ordering
        if (!btn) return null;
        await clickAndWait(btn, 400);
        const after = rowsByAnchor("school", "degree").length
          ? rowsByAnchor("school", "degree")
          : rowsByAnchor("schoolName", "degree");
        return after[after.length - 1] || null;
      },
      perRowMap: (item) => {
        const start = normalizedParts(item, "start");
        const end = normalizedParts(item, "end");
        return {
          school:         item.school,
          degree:         item.degree,
          field_of_study: item.field_of_study,
          gpa:            item.gpa != null ? String(item.gpa) : "",
          start_year:     start.year,
          end_year:       end.year,
        };
      },
      perRowFieldMap: {
        school: (row) => firstMatch(row, [
          "[data-automation-id='formField-school'] input",
          "[data-automation-id='formField-schoolName'] input",
          "[data-automation-id='formField-school'] button",  // some tenants make this a typeahead
        ]),
        degree: (row) => firstMatch(row, [
          "[data-automation-id='formField-degree'] input",
          "[data-automation-id='formField-degree'] button",
        ]),
        field_of_study: (row) => firstMatch(row, [
          "[data-automation-id='formField-fieldOfStudy'] input",
          "[data-automation-id='formField-fieldOfStudy'] button",
        ]),
        gpa: (row) => firstMatch(row, [
          "[data-automation-id='formField-gradeAverage'] input",
          "[data-automation-id='formField-gpa'] input",
        ]),
        start_year: (row) => firstMatch(row, [
          "[data-automation-id='formField-fromDate'] [data-automation-id='dateSectionYear-input']",
          "[data-automation-id='formField-startDate'] [data-automation-id='dateSectionYear-input']",
        ]),
        end_year: (row) => firstMatch(row, [
          "[data-automation-id='formField-toDate'] [data-automation-id='dateSectionYear-input']",
          "[data-automation-id='formField-endDate'] [data-automation-id='dateSectionYear-input']",
        ]),
      },
    },
  ],

  findResumeInput(root) {
    return root.querySelector("[data-automation-id='file-upload-input-ref']")
      || root.querySelector("input[type=file]");
  },
};
