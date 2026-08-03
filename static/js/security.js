(function () {
    "use strict";

    const meta = document.querySelector('meta[name="csrf-token"]');
    const token = meta ? meta.getAttribute("content") : "";
    const unsafeMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);

    function isSameOriginRequest(input) {
        try {
            const rawUrl = typeof input === "string" ? input : input.url;
            return new URL(rawUrl, window.location.href).origin === window.location.origin;
        } catch (_error) {
            return false;
        }
    }

    function protectForms() {
        if (!token) return;
        document.querySelectorAll("form").forEach(function (form) {
            const method = (form.getAttribute("method") || "GET").toUpperCase();
            if (!unsafeMethods.has(method)) return;
            if (form.querySelector('input[name="_csrf_token"]')) return;
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "_csrf_token";
            input.value = token;
            form.prepend(input);
        });
    }

    if (token && window.fetch) {
        const originalFetch = window.fetch.bind(window);
        window.fetch = function (input, init) {
            const options = Object.assign({}, init || {});
            const requestMethod = (
                options.method || (typeof input !== "string" && input.method) || "GET"
            ).toUpperCase();
            if (unsafeMethods.has(requestMethod) && isSameOriginRequest(input)) {
                const headers = new Headers(
                    options.headers || (typeof input !== "string" ? input.headers : undefined)
                );
                if (!headers.has("X-CSRF-Token")) {
                    headers.set("X-CSRF-Token", token);
                }
                options.headers = headers;
                options.credentials = options.credentials || "same-origin";
            }
            return originalFetch(input, options);
        };
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", protectForms, {once: true});
    } else {
        protectForms();
    }
})();
