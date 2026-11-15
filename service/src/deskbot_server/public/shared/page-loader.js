(async function () {
  "use strict";
  try {
    const dataUrl = "/api/pages" + location.pathname + location.search;
    const response = await fetch(dataUrl, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (response.status === 401) {
      location.replace("/login?next=" + encodeURIComponent(location.pathname + location.search));
      return;
    }
    const payload = await response.json();
    if (payload.redirect) { location.replace(payload.redirect); return; }
    if (!response.ok) throw new Error(payload.error || "页面暂时无法加载");
    const viewsResponse = await fetch("/static/views.json", { cache: "no-store" });
    if (!viewsResponse.ok) throw new Error("页面资源加载失败");
    const html = DeskbotPages.render(await viewsResponse.json(), payload);
    document.open();
    document.write(html);
    document.close();
  } catch (error) {
    document.getElementById("page-status").textContent = error.message || "页面暂时无法加载";
  }
})();
