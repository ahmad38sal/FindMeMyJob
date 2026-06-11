// Lever — jobs.lever.co/<company>/<job-id>/apply
//
// Inputs are server-rendered with stable name= attrs: name="name", name="email",
// name="phone", name="resume" (file), name="urls[LinkedIn]", name="urls[GitHub]",
// name="urls[Portfolio]". Lever uses one combined "name" field — we map both
// first/last into full_name's adapter slot.
//
// supported_keys: full_name, email, phone, linkedin_url, github_url, portfolio_url, resume_file
// unsupported_keys: custom-question text fields (vary per posting), cover_letter

export default {
  name: "lever",
  urlPattern: "*://jobs.lever.co/*",
  fieldMap: {
    full_name:     "input[name='name']",
    email:         "input[name='email']",
    phone:         "input[name='phone']",
    linkedin_url:  "input[name='urls[LinkedIn]']",
    github_url:    "input[name='urls[GitHub]']",
    portfolio_url: "input[name='urls[Portfolio]']",
  },
  findResumeInput(root) {
    return root.querySelector("input[name='resume']")
      || root.querySelector("input[type=file][accept*='pdf']");
  },
};
