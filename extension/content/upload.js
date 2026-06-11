// File-input attach. <input type=file>.value is read-only for security;
// the only way to populate it programmatically is via DataTransfer, which
// is what drag-and-drop uses internally — browsers treat it as if the user
// dropped the file in.

import { flash } from "./engine.js";

function rebuildBlob(envelope) {
  const bytes = new Uint8Array(envelope.bytes);
  return new Blob([bytes], { type: envelope.mimeType || "application/pdf" });
}

export async function attachResume(envelope, adapter, root = document) {
  if (!adapter || typeof adapter.findResumeInput !== "function") {
    return { ok: false, reason: "adapter has no findResumeInput()" };
  }
  let input = null;
  try { input = adapter.findResumeInput(root); }
  catch (e) { return { ok: false, reason: `findResumeInput threw: ${e.message}` }; }
  if (!input) return { ok: false, reason: "no resume <input type=file> on page" };

  let file;
  try {
    const blob = rebuildBlob(envelope);
    file = new File([blob], envelope.filename || "resume.pdf", { type: blob.type });
  } catch (e) {
    return { ok: false, reason: `blob/file build failed: ${e.message}` };
  }

  try {
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    flash(input);
  } catch (e) {
    return { ok: false, reason: `DataTransfer set failed: ${e.message}` };
  }

  if (typeof adapter.afterAttachResume === "function") {
    try { await adapter.afterAttachResume(root, file); } catch (_) {}
  }
  return { ok: true };
}
