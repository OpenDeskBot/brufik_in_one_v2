/* Static pages load their JSON context on navigation. */
(function () {
  "use strict";
  window.__appShellNavigate = function (url) {
    window.location.assign(new URL(url, window.location.origin));
    return Promise.resolve();
  };
})();
