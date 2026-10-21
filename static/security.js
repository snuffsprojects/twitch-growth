(() => {
  const config = window.TWITCH_GROWTH || {};
  const originalFetch = window.fetch.bind(window);
  window.fetch = (resource, options = {}) => {
    const method = String(options.method || "GET").toUpperCase();
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && config.csrf) {
      const headers = new Headers(options.headers || {});
      headers.set("X-CSRF-Token", config.csrf);
      options = { ...options, headers };
    }
    return originalFetch(resource, options).then(response => {
      if (response.status === 401 && !location.pathname.endsWith("/login")) {
        location.assign(`${config.base || ""}/login?next=${encodeURIComponent(location.pathname)}`);
      }
      return response;
    });
  };
})();
