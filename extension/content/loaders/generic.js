// Generic fallback. Runs on <all_urls> per the manifest, but yields to a
// dedicated adapter via the synchronous slot claim, AND skips known-ATS
// hostnames outright (defense in depth: in case the dedicated content
// script is registered but happens to load *after* generic).
const KNOWN_ATS = /(?:myworkdayjobs\.com|greenhouse\.io|lever\.co|jobs\.apple\.com|icims\.com|smartrecruiters\.com|ashbyhq\.com|jobvite\.com)$/i;

if (window.__findmemyjob_loader) {
  // dedicated adapter already in charge
} else if (KNOWN_ATS.test(location.hostname)) {
  // dedicated adapter is on its way (or should be) — don't fire generic
} else {
  window.__findmemyjob_loader = "generic";
  (async () => {
    const [engine, upload, adapterMod] = await Promise.all([
      import(chrome.runtime.getURL("content/engine.js")),
      import(chrome.runtime.getURL("content/upload.js")),
      import(chrome.runtime.getURL("content/adapters/generic.js")),
    ]);
    const adapter = adapterMod.default;
    engine.registerMessageListener(adapter, upload);
    // Don't auto-match on <all_urls> — too noisy. Popup will trigger match
    // explicitly when the user opens it on a site they care about.
  })();
}
