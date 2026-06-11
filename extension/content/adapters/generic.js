// Generic fallback — runs on every site that isn't covered by a dedicated
// adapter. Walks visible inputs and matches each canonical key against label
// text / aria-label / placeholder / name / id with a regex.
//
// This is intentionally conservative — better to miss a field than to fill
// the wrong one. Add ATS-specific adapters when you find a site this misses.

const REGEX_BY_KEY = {
  first_name:   /(^|[^a-z])(first[\s_-]?name|given[\s_-]?name|fname)([^a-z]|$)/i,
  last_name:    /(^|[^a-z])(last[\s_-]?name|family[\s_-]?name|surname|lname)([^a-z]|$)/i,
  full_name:    /(^|[^a-z])(full[\s_-]?name|your[\s_-]?name|^name$)([^a-z]|$)/i,
  email:        /e[\s_-]?mail/i,
  phone:        /(phone|mobile|telephone|tel\b)/i,
  linkedin_url: /linked[\s_-]?in/i,
  github_url:   /github/i,
  portfolio_url:/(portfolio|website|personal[\s_-]?site)/i,
  city:         /\bcity\b/i,
  region:       /(state|region|province)/i,
  postal_code:  /(zip|postal[\s_-]?code|postcode)/i,
  country:      /country/i,
  current_company: /(current[\s_-]?employer|current[\s_-]?company)/i,
  current_title:   /(current[\s_-]?title|current[\s_-]?role|job[\s_-]?title)/i,
};

function fieldHaystack(el) {
  const id = el.id || "";
  const name = el.getAttribute("name") || "";
  const placeholder = el.getAttribute("placeholder") || "";
  const aria = el.getAttribute("aria-label") || "";
  let label = "";
  if (id) {
    const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
    if (lab) label = lab.textContent || "";
  }
  if (!label) {
    const wrap = el.closest("label");
    if (wrap) label = wrap.textContent || "";
  }
  return [label, aria, placeholder, name, id].join(" ");
}

function matchesKey(el, key) {
  const re = REGEX_BY_KEY[key];
  if (!re) return false;
  return re.test(fieldHaystack(el));
}

export default {
  name: "generic",
  urlPattern: "*",
  fieldMap: Object.fromEntries(
    Object.keys(REGEX_BY_KEY).map((key) => [key, (root) => {
      const candidates = root.querySelectorAll(
        "input[type=text], input[type=email], input[type=tel], input[type=url], input:not([type]), textarea"
      );
      for (const el of candidates) {
        if (el.disabled || el.readOnly) continue;
        if (matchesKey(el, key)) return el;
      }
      return null;
    }])
  ),
  findResumeInput(root) {
    return root.querySelector("input[type=file][name*='resume' i]")
      || root.querySelector("input[type=file][accept*='pdf']")
      || root.querySelector("input[type=file]");
  },
};
