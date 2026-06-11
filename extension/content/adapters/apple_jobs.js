// Apple jobs (external) — jobs.apple.com
//
// External Apple Jobs is its own custom React app. Selectors below are
// EDUCATED PLACEHOLDERS — verify against an actual jobs.apple.com application
// page and tune. The generic adapter's regex fallback should also cover most
// of these labels if the explicit selectors miss.
//
// supported_keys (TODO — verify against a live application page):
//   first_name, last_name, email, phone, linkedin_url, resume_file
// unsupported_keys:
//   work-auth radio groups, EEO / self-identify, multi-page nav

export default {
  name: "apple_jobs",
  urlPattern: "*://jobs.apple.com/*",
  fieldMap: {
    // TODO: confirm these IDs once you hit an actual jobs.apple.com apply form.
    first_name:   "input[name='firstName'], input#firstName, input[id*='first' i]",
    last_name:    "input[name='lastName'],  input#lastName,  input[id*='last' i]",
    email:        "input[type=email], input[name='email']",
    phone:        "input[type=tel],   input[name='phone']",
    linkedin_url: "input[name*='linkedin' i]",
  },
  findResumeInput(root) {
    return root.querySelector("input[type=file][accept*='pdf']")
      || root.querySelector("input[type=file][name*='resume' i]")
      || root.querySelector("input[type=file]");
  },
};
