/**
 * auth/guard.js
 *
 * Se incluye SOLO en base-datos.html, antes de js/base-datos.js.
 * - Comprueba sesión contra /api/auth/me. Si no hay, redirige a login.
 * - Expone window.apiFetch (fetch con cookie) y window.esc (anti-XSS)
 *   para que los scripts de la página los usen sin repetir código.
 */

(async () => {
  try {
    const r = await fetch("/api/auth/me", { credentials: "include" });
    if (r.status === 401) {
      location.replace("auth/login.html");
      return;
    }
    document.documentElement.dataset.authed = "1";
  } catch {
    location.replace("auth/login.html");
  }
})();

/** fetch con cookie de sesión incluida. */
window.apiFetch = (url, opts = {}) =>
  fetch(url, { credentials: "include", ...opts });

/** Escapa HTML para evitar XSS al inyectar datos del servidor. */
window.esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));