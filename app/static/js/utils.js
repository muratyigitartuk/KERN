/**
 * Shared utility functions for KERN dashboard modules.
 */

/**
 * @deprecated No-op retained for call-site compatibility. Token exchange
 * is now handled server-side via httpOnly session cookies.
 */
export function bootstrapAdminAuthToken() {
  // Clean up any legacy localStorage token from previous versions.
  localStorage.removeItem("kern.admin.token");
}

/**
 * @deprecated Always returns empty string. Auth is cookie-based now.
 */
export function getAdminAuthToken() {
  return "";
}

/**
 * @deprecated Identity function. Token is no longer appended to URLs.
 */
export function withAdminToken(url) {
  return url;
}

/**
 * Escape HTML special characters to prevent XSS when inserting into innerHTML.
 * @param {string} str
 * @returns {string}
 */
export function escapeHTML(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
    .replace(/`/g, "&#96;");  // H-18: Also escape backticks.
}

/**
 * Read the CSRF token from the cookie set by the server.
 * @returns {string}
 */
export function getCSRFToken() {
  const match = document.cookie.match(/(?:^|;\s*)kern_csrf_token=([^;]+)/);
  return match ? match[1] : "";
}

/**
 * Wrapper around fetch() that automatically adds the CSRF token header
 * for state-changing requests (POST, PUT, DELETE, PATCH).
 * @param {string|Request} url
 * @param {RequestInit} [options]
 * @returns {Promise<Response>}
 */
export function secureFetch(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (["POST", "PUT", "DELETE", "PATCH"].includes(method)) {
    headers.set("x-csrf-token", getCSRFToken());
  }
  options.headers = headers;
  options.credentials = "same-origin";
  return fetch(url, options);
}

/**
 * Central error reporter.  Logs to the browser console with a [KERN]
 * prefix so errors are easy to filter.
 * @param {string} context - Short label describing where the error occurred
 * @param {*} error - The error object or message
 */
export function reportError(context, error) {
  console.error(`[KERN] ${context}:`, error);
}
