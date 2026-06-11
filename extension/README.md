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
| Workday     | `*.myworkdayjobs.com/*`              | Adapter scaffolded — multi-page nav not handled |
| Apple Jobs  | `jobs.apple.com/*` (external)        | Adapter scaffolded — selectors need real-page tuning |
| Generic     | every other site                     | Heuristic regex fallback |

## What's intentionally *not* in this scaffold

- Auto-submit. The extension never clicks "Submit" — it fills, attaches, and
  flashes the field outline so you can spot-check before you click.
- EEO / demographics autofill.
- Workday's multi-step "Apply Manually" flow (account creation, "Save and
  Continue" pagination).
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
