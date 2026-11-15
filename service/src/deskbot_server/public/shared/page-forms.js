/* Account forms use JSON APIs; business page forms keep their existing Vue handlers. */
(function () {
  "use strict";
  if (window.__deskbotFormsInstalled) return;
  window.__deskbotFormsInstalled = true;
  document.addEventListener("submit", async function (event) {
    const form = event.target;
    const target = new URL(form.action || location.href, location.origin);
    if (target.origin !== location.origin || !["/login", "/register", "/logout"].includes(target.pathname)) return;
    event.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      const response = await fetch(target, { method: "POST", body: new FormData(form), credentials: "same-origin",
        headers: { Accept: "application/json" } });
      const payload = await response.json();
      if (payload.redirect) { location.assign(payload.redirect); return; }
      const message = document.getElementById("form-status") || document.createElement("p");
      message.id = "form-status";
      message.setAttribute("role", "alert");
      message.textContent = payload.context && payload.context.flashes
        ? payload.context.flashes.map((row) => row[1]).join("；") : (payload.error || "操作失败，请重试");
      form.appendChild(message);
    } catch (_) {
      const message = document.createElement("p");
      message.setAttribute("role", "alert");
      message.textContent = "连接失败，请重试";
      form.appendChild(message);
    } finally { if (submit) submit.disabled = false; }
  });
})();
