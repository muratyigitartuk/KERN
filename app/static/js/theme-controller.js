const THEME_MODE_KEY = "kern.theme.mode";
const DARK_THEME_COLOR = "#171717";
const LIGHT_THEME_COLOR = "#f5efe3";
const VALID_MODES = new Set(["system", "dark", "light"]);

function normalizePreference(value) {
  return VALID_MODES.has(value) ? value : "system";
}

function resolveActiveTheme(preference, prefersDark) {
  return preference === "system" ? (prefersDark ? "dark" : "light") : preference;
}

export function createThemeController() {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const listeners = new Set();
  let preference = normalizePreference(localStorage.getItem(THEME_MODE_KEY));
  let activeTheme = resolveActiveTheme(preference, media.matches);

  function apply(theme) {
    document.documentElement.dataset.theme = theme;
    const meta = document.getElementById("themeColorMeta");
    if (meta) {
      meta.setAttribute("content", theme === "dark" ? DARK_THEME_COLOR : LIGHT_THEME_COLOR);
    }
  }

  function emit() {
    const state = controller.getState();
    listeners.forEach((listener) => listener(state));
  }

  function sync() {
    activeTheme = resolveActiveTheme(preference, media.matches);
    apply(activeTheme);
    emit();
  }

  function handleSystemChange() {
    if (preference !== "system") {
      return;
    }
    sync();
  }

  if (typeof media.addEventListener === "function") {
    media.addEventListener("change", handleSystemChange);
  } else if (typeof media.addListener === "function") {
    media.addListener(handleSystemChange);
  }

  apply(activeTheme);

  const controller = {
    getPreference() {
      return preference;
    },
    getActiveTheme() {
      return activeTheme;
    },
    getState() {
      return {
        preference,
        activeTheme,
      };
    },
    setPreference(nextPreference) {
      preference = normalizePreference(nextPreference);
      localStorage.setItem(THEME_MODE_KEY, preference);
      sync();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };

  return controller;
}
