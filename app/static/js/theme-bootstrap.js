const storageKey = "kern.theme.mode";
const stored = localStorage.getItem(storageKey);
const preference = stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
const resolved = preference === "system" ? (prefersDark ? "dark" : "light") : preference;

document.documentElement.dataset.theme = resolved;

const meta = document.getElementById("themeColorMeta");
if (meta) {
  meta.setAttribute("content", resolved === "dark" ? "#111111" : "#f5f5f5");
}
