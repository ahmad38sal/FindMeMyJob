// Synchronously claim the slot BEFORE any await so the generic loader (or
// any later loader) sees the flag and bails. Otherwise both content scripts
// reach the guard in parallel during their dynamic imports.
if (window.__findmemyjob_loader) { /* already claimed */ } else {
  window.__findmemyjob_loader = "greenhouse";
  (async () => {
    const [engine, upload, adapterMod] = await Promise.all([
      import(chrome.runtime.getURL("content/engine.js")),
      import(chrome.runtime.getURL("content/upload.js")),
      import(chrome.runtime.getURL("content/adapters/greenhouse.js")),
    ]);
    const adapter = adapterMod.default;
    engine.registerMessageListener(adapter, upload);
    chrome.runtime.sendMessage({
      kind: "match-current-page",
      url: location.href,
      page_title: document.title,
      company: location.pathname.split("/").filter(Boolean)[0] || null,
    });
  })();
}
