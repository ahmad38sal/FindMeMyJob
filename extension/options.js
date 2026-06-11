import { getSettings, setSettings, health } from "./lib/api.js";

const $ = (id) => document.getElementById(id);

(async function init() {
  const { backend_url, token } = await getSettings();
  $("backend_url").value = backend_url || "";
  $("token").value = token || "";
})();

$("save").addEventListener("click", async () => {
  await setSettings({
    backend_url: $("backend_url").value.trim().replace(/\/+$/, ""),
    token: $("token").value.trim(),
  });
  flash("Saved.", "ok");
});

$("test").addEventListener("click", async () => {
  flash("Testing…", "");
  // Save first so health() picks up the latest values.
  await setSettings({
    backend_url: $("backend_url").value.trim().replace(/\/+$/, ""),
    token: $("token").value.trim(),
  });
  try {
    const data = await health();
    flash(`Connected — ${data.service} v${data.version}`, "ok");
  } catch (e) {
    flash(`${e.kind || "Error"}: ${e.message}`, "fail");
  }
});

function flash(msg, cls) {
  const s = $("status");
  s.textContent = msg;
  s.className = "status " + cls;
}
