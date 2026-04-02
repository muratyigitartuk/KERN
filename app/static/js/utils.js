/**
 * Shared utility functions for KERN dashboard modules.
 */
const ADMIN_TOKEN_STORAGE_KEY = "kern.admin.token";

export function bootstrapAdminAuthToken() {
  const url = new URL(window.location.href);
  const queryToken = url.searchParams.get("token");
  if (queryToken) {
    localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, queryToken);
    url.searchParams.delete("token");
    window.history.replaceState({}, document.title, `${url.pathname}${url.search}${url.hash}`);
    return queryToken;
  }
  return localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
}

export function getAdminAuthToken() {
  return localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
}

export function withAdminToken(url) {
  const token = getAdminAuthToken();
  if (!token) {
    return url;
  }
  const resolved = new URL(url, window.location.origin);
  resolved.searchParams.set("token", token);
  if (resolved.origin === window.location.origin) {
    return `${resolved.pathname}${resolved.search}${resolved.hash}`;
  }
  return resolved.toString();
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
    .replace(/'/g, "&#39;");
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
  const adminToken = getAdminAuthToken();
  if (adminToken) {
    headers.set("authorization", `Bearer ${adminToken}`);
  }
  if (["POST", "PUT", "DELETE", "PATCH"].includes(method)) {
    headers.set("x-csrf-token", getCSRFToken());
  }
  options.headers = headers;
  return fetch(withAdminToken(url), options);
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
