# FindMeMyJob — Browser Extension

MV3 Chrome extension. Reads job-fit / tailored-resume info from the FindMeMyJob
backend, attaches the right resume to ATS forms, and autofills the rest.

## Setup

1. **Backend:** set `FINDMEMYJOB_EXT_TOKEN` in your project `.env` (any random
   string). Restart `uv run uvicorn findmemyjob.main:app --reload`.
2. **Icons:** drop `icon-16.png` / `icon-48.png` / `icon-128.png` into
   `extension/icons/` (see `icons/README.md`) — Chrome will refuse to load the
   unpacked extension without these.
3. **Load unpacked:** `chrome://extensions` → toggle Developer mode →
   "Load unpacked" → select this `extension/` directory.
4. **Configure:** right-click the extension icon → Options. Paste:
   - Backend URL: `http://localhost:8000` (or your prod host)
   - Bearer token: same value as `FINDMEMYJOB_EXT_TOKEN`
   Click "Test connection" — should say "Connected".

## ATS coverage (this scaffold)

| ATS         | URL pattern                          | Status                 |
|-------------|--------------------------------------|------------------------|
| Greenhouse  | `boards.greenhouse.io/*`             | Adapter scaffolded     |
| Lever       | `jobs.lever.co/*`                    | Adapter scaffolded     |
| Workday     | `*.myworkdayjobs.com/*`              | **Autofill implemented** — normalized dates, repeating rows, multi-page nav |
| Apple Jobs  | `jobs.apple.com/*` (external)        | Adapter scaffolded — selectors need real-page tuning |
| Generic     | every other site                     | Heuristic regex fallback |

## Workday autofill

Workday is the priority ATS. The Workday adapter
(`content/adapters/workday.js`) fetches **normalized, ATS-safe** application
data from the backend endpoint `GET /api/ext/application-data/{job_id}`
(assembled by `src/findmemyjob/ats.py`) instead of the raw autofill payload.
This matters because Workday parses uploaded resumes badly — the normalized
endpoint hands the extension clean company names, standardized titles, a
consistent phone format, and every date split into **month + year + a
"current" bool**, which is exactly Workday's date-select + "I currently work
here" checkbox shape.

### `data-automation-id` selectors used

Workday tenants vary, so each field tries several selectors (see the adapter
for the full fallback lists). The primary ones:

| Field                | Selector (primary)                                                            |
|----------------------|-------------------------------------------------------------------------------|
| First / last name    | `formField-firstName input`, `formField-lastName input`                       |
| Email                | `formField-email input`                                                       |
| Phone                | `formField-phone-number input` / `formField-phoneNumber input`                |
| Address / city / zip | `formField-addressLine1`, `formField-city`, `formField-postalCode` (`input`)  |
| Country / region     | `formField-country button`, `formField-countryRegion button` (listbox popup)  |
| Work row container    | smallest ancestor of `formField-jobTitle` also holding `formField-companyName` |
| Job title / company  | `formField-jobTitle input\|textarea`, `formField-companyName input`           |
| Work location        | `formField-location input`                                                     |
| From / To month      | `formField-startDate\|endDate > dateSectionMonth-input`                        |
| From / To year       | `formField-startDate\|endDate > dateSectionYear-input`                         |
| "Currently work here"| `formField-currentlyWorkHere input[type=checkbox]`                            |
| Role description     | `formField-roleDescription textarea`                                          |
| Add another row      | `[data-automation-id="add-button"]` (Nth = section order: work, edu, …)        |
| Education            | `formField-school`, `formField-degree`, `formField-fieldOfStudy`, `formField-gradeAverage` |
| Resume upload        | `file-upload-input-ref`                                                        |
| Advance / next       | `pageFooterNextButton` (never a Submit button)                                |

**React-controlled inputs:** Workday is React, so setting `el.value` alone is
reverted on the next render. The engine uses the native prototype value setter
(`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set`) then
dispatches `input` + `change` — the trick React itself uses internally.
Dropdowns/listbox comboboxes are opened by click, then the matching option text
is clicked. Filled fields get a blue outline flash. **Nothing is auto-submitted**
— auto-apply stops at the Submit page for you to review.

### Manual test checklist (Workday)

1. Load the extension unpacked and set backend URL + token in Options.
2. In FindMeMyJob, track a Workday role and tailor a resume for it so
   `GET /api/ext/application-data/{job_id}` has data (verify with
   `curl -H "Authorization: Bearer $TOKEN" $BACKEND/api/ext/application-data/<id>`).
3. Open a `*.myworkdayjobs.com` posting → **Apply / Apply Manually** → create or
   sign in to the candidate account until you reach **My Information**.
4. Open the extension popup → **Autofill** (or **Auto-apply** for multi-page).
   Expect: first/last name, email, phone, address, city, postal, country/region
   filled and outlined.
5. Continue to **My Experience**. Expect each work row filled: title, company,
   location, From month+year, To month+year (or "I currently work here" checked
   with the To-date left blank for your current role). If you have more roles
   than visible rows, the engine clicks **Add Another** first.
6. Check **dates** specifically — the whole point of the normalized endpoint.
   Messy stored dates ("January 2023", "01/2023", "2023-01") should all land as
   the correct month + year.
7. Confirm the extension **never** clicks Submit; the status banner reports
   "Filled N fields … review then submit yourself".
8. Per-field failures are logged to the page console under `[FindMeMyJob]` and
   reported in the popup's skipped list — they never abort the whole fill.

## What's intentionally *not* in this scaffold

- Auto-submit. The extension never clicks "Submit" — it fills, attaches, and
  flashes the field outline so you can spot-check before you click.
- EEO / demographics autofill.
- Workday account creation (you must sign in / create the candidate account
  yourself before the "My Information" page appears).
- Apple internal careers (server-side only — needs AppleConnect; lives in the
  FastAPI app, not the extension).
- LLM fallback for unknown fields (planned: route through the backend's
  Floodgate proxy).

## Project layout

```
extension/
├── manifest.json
├── background.js              # service worker (module)
├── lib/api.js                 # bearer-token fetch wrapper
├── popup.html / popup.js / popup.css
├── options.html / options.js
├── content/
│   ├── engine.js              # walks fieldMap, dispatches React-safe value setters
│   ├── upload.js              # DataTransfer-based file attach
│   ├── adapters/{greenhouse,lever,workday,apple_jobs,generic}.js
│   └── loaders/{greenhouse,lever,workday,apple_jobs,generic}.js
└── icons/
```
