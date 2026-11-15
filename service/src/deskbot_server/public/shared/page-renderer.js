/* Browser-owned rendering. This same pure renderer is used by the UI regression harness. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./vendor/nunjucks-3.2.4.min.js"));
  } else root.DeskbotPages = factory(root.nunjucks);
})(typeof globalThis !== "undefined" ? globalThis : this, function (nunjucks) {
  "use strict";
  function environment(views, payload) {
    const Loader = nunjucks.Loader.extend({
      getSource(name) {
        if (!Object.prototype.hasOwnProperty.call(views, name)) return null;
        return { src: views[name], path: name, noCache: true };
      },
    });
    const env = new nunjucks.Environment(new Loader(), { autoescape: true });
    env.addFilter("tojson", function (value) {
      const json = JSON.stringify(value === undefined ? null : value)
        .replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/&/g, "\\u0026")
        .replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
      return new nunjucks.runtime.SafeString(json);
    });
    env.addFilter("displaydate", (value) => value ? String(value).replace("T", " ").slice(0, 16) : "—");
    env.addGlobal("url_for", function (name, values) {
      values = Object.assign({}, values || {});
      delete values.__keywords;
      if (name === "static") return "/static/" + String(values.filename || "").replace(/^\/+/, "");
      let path = (payload.endpoints || {})[name];
      if (!path) throw new Error("Unknown endpoint: " + name);
      const query = new URLSearchParams();
      Object.entries(values).forEach(([key, value]) => {
        if (path.includes("{" + key + "}")) path = path.replace("{" + key + "}", encodeURIComponent(value));
        else if (value !== null && value !== undefined) query.set(key, value);
      });
      return path + (query.size ? "?" + query : "");
    });
    env.addGlobal("get_flashed_messages", () => payload.context.flashes || []);
    return env;
  }
  function render(views, payload) {
    if (!payload || !payload.template || !payload.context) throw new Error("Invalid page response");
    const html = environment(views, payload).render(payload.template, payload.context);
    return html.replace(/<\/body>/i, '<script src="/static/page-forms.js"></script></body>');
  }
  return { render };
});
