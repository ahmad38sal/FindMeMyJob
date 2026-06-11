if (window.__findmemyjob_loader) { /* already claimed */ } else {
  window.__findmemyjob_loader = "lever";
  (async () => {
    const [engine, upload, adapterMod] = await Promise.all([
      import(chrome.runtime.getURL("content/engine.js")),
      import(chrome.runtime.getURL("content/upload.js")),
      import(chrome.runtime.getURL("content/adapters/lever.js")),
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
