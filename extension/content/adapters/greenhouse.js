// Greenhouse — boards.greenhouse.io/<company>/jobs/<id>
//
// Form is a plain server-rendered HTML form: inputs have stable id="first_name",
// id="last_name", etc. Resume is a real <input type=file id="resume">.
//
// supported_keys: first_name, last_name, email, phone, linkedin_url, website_url, resume_file
// unsupported_keys: cover_letter (custom-questions vary per posting), demographics

export default {
  name: "greenhouse",
  urlPattern: "*://boards.greenhouse.io/*",
  fieldMap: {
    first_name:  "input#first_name",
    last_name:   "input#last_name",
    email:       "input#email",
    phone:       "input#phone",
    linkedin_url:(root) => root.querySelector("input[id$='linkedin']") || root.querySelector("input[name*='linkedin' i]"),
    website_url: (root) => root.querySelector("input[id$='website']") || root.querySelector("input[name*='website' i]") || root.querySelector("input[name*='portfolio' i]"),
  },
  findResumeInput(root) {
    return root.querySelector("input#resume")
      || root.querySelector("input[type=file][name*='resume' i]")
      || root.querySelector("input[type=file][accept*='pdf']");
  },
};
