/**
 * KERN Internationalization Module
 *
 * Lightweight i18n system for the KERN dashboard.
 * Loads locale JSON files and provides a t() function for string lookup.
 */

const _i18n = {
  _strings: {},
  _lang: "en",
  _loaded: false,
};

function applyText(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });

  root.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.setAttribute("placeholder", t(element.dataset.i18nPlaceholder));
  });

  root.querySelectorAll("[data-i18n-title]").forEach((element) => {
    element.setAttribute("title", t(element.dataset.i18nTitle));
  });

  root.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
    element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
  });

  root.querySelectorAll("[data-i18n-settings-title]").forEach((element) => {
    element.dataset.settingsTitle = t(element.dataset.i18nSettingsTitle);
  });

  root.querySelectorAll("[data-i18n-prompt]").forEach((element) => {
    element.dataset.prompt = t(element.dataset.i18nPrompt);
  });
}

/**
 * Load a locale file and set it as active.
 * @param {string} lang - Language code (e.g., "en", "de")
 * @returns {Promise<void>}
 */
async function loadLocale(lang) {
  try {
    const resp = await fetch(`/static/locales/${lang}.json`);
    if (!resp.ok) {
      console.warn(`[i18n] Locale '${lang}' not found, falling back to 'en'.`);
      if (lang !== "en") return loadLocale("en");
      return;
    }
    _i18n._strings = await resp.json();
    _i18n._lang = lang;
    _i18n._loaded = true;
    document.documentElement.lang = lang;
  } catch (err) {
    console.warn(`[i18n] Failed to load locale '${lang}':`, err);
    if (lang !== "en") return loadLocale("en");
  }
}

/**
 * Translate a key to the current locale string.
 * Supports simple interpolation: t("key", { count: 5 }) replaces ${count} in the string.
 * Falls back to the key itself if not found (so English keys are readable).
 * @param {string} key - Dot-separated locale key
 * @param {Object} [params] - Interpolation parameters
 * @returns {string}
 */
function t(key, params) {
  let str = _i18n._strings[key];
  if (str === undefined || str === null) return key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      str = str.replace(new RegExp("\\$\\{" + k + "\\}", "g"), String(v));
    }
  }
  return str;
}

/**
 * Get the current active language code.
 * @returns {string}
 */
function getCurrentLang() {
  return _i18n._lang;
}

/**
 * Check if locales have been loaded.
 * @returns {boolean}
 */
function isLocaleLoaded() {
  return _i18n._loaded;
}

export { loadLocale, t, getCurrentLang, isLocaleLoaded, applyText };
