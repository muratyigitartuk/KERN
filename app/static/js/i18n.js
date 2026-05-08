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

const _CRITICAL_FALLBACK_STRINGS = {
  en: {
    "empty.heading": "Ready to Help.",
    "empty.heading_alt_1": "Ready when you are.",
    "empty.heading_alt_2": "What should we work on first?",
    "empty.heading_alt_3": "Let's make this useful.",
    "composer.placeholder": "What do you want to work on?",
    "composer.aria_label": "Write a message",
    "session.no_items": "No recent chats yet.",
    "session.noitems": "No recent chats yet.",
    "session.current": "Current conversation",
    "session.turns": "${count} turns",
    "session.untitled": "Untitled conversation",
    "session.actions": "Conversation actions",
    "session.rename": "Rename",
    "session.pin": "Pin",
    "session.delete": "Delete",
    "session.delete_confirm_title": "Delete conversation?",
    "session.delete_confirm_body": "\"${title}\" will be removed from the sidebar.",
    "settings.language_english": "English",
    "settings.language_german": "Deutsch",
    "workspace.create_title": "New workspace",
    "workspace.create_prompt": "Name the new workspace",
    "workspace.rename_title": "Rename workspace",
    "workspace.rename_prompt": "Rename this workspace",
    "confirm.cancel": "Cancel",
    "confirm.create": "Create",
    "confirm.save": "Save",
    "prompts.summarize_document": "Summarize my latest local document and list the most important risks, dates, and open points.",
    "prompts.review_compliance": "Review this document or workflow for compliance risks, missing controls, and the clearest next actions.",
    "prompts.analyze_risk": "Analyze the main risks in this document or workflow, then rank them by impact and urgency.",
    "prompts.create_policy": "Draft a short workplace policy with clear rules, scope, and owner responsibilities. If key details are missing, ask concise follow-up questions first.",
  },
  de: {
    "empty.heading": "Bereit zu helfen.",
    "empty.heading_alt_1": "Bereit, wenn du es bist.",
    "empty.heading_alt_2": "Womit legen wir los?",
    "empty.heading_alt_3": "Lass uns etwas daraus machen.",
    "composer.placeholder": "Woran willst du gerade arbeiten?",
    "composer.aria_label": "Nachricht schreiben",
    "session.no_items": "Noch nichts im Verlauf.",
    "session.noitems": "Noch nichts im Verlauf.",
    "session.current": "Aktuelles Gespräch",
    "session.turns": "${count} Schritte",
    "session.untitled": "Unbenanntes Gespräch",
    "session.actions": "Gesprächsaktionen",
    "session.rename": "Umbenennen",
    "session.pin": "Anheften",
    "session.delete": "Löschen",
    "session.delete_confirm_title": "Gespräch löschen?",
    "session.delete_confirm_body": "\"${title}\" wird aus dieser Seitenleiste entfernt.",
    "settings.language_english": "Englisch",
    "settings.language_german": "Deutsch (DE)",
    "workspace.create_title": "Neuer Arbeitsbereich",
    "workspace.create_prompt": "Wie soll der neue Arbeitsbereich heißen?",
    "workspace.rename_title": "Arbeitsbereich umbenennen",
    "workspace.rename_prompt": "Arbeitsbereich umbenennen",
    "confirm.cancel": "Abbrechen",
    "confirm.create": "Erstellen",
    "confirm.save": "Speichern",
    "prompts.summarize_document": "Fasse mein neuestes lokales Dokument kurz zusammen und nenne die wichtigsten Risiken, Termine und offenen Punkte.",
    "prompts.review_compliance": "Prüfe dieses Dokument oder diesen Ablauf auf Compliance-Risiken, fehlende Kontrollen und die klarsten nächsten Schritte.",
    "prompts.analyze_risk": "Analysiere die wichtigsten Risiken in diesem Dokument oder Ablauf und ordne sie nach Auswirkung und Dringlichkeit.",
    "prompts.create_policy": "Entwirf eine kurze Arbeitsplatzrichtlinie mit klaren Regeln, Geltungsbereich und Zuständigkeiten. Wenn wichtige Details fehlen, stelle zuerst knappe Rückfragen.",
  },
};

const _KEY_ALIASES = {
  "session.noitems": "session.no_items",
};

function resolveKey(key) {
  return _KEY_ALIASES[key] || key;
}

function resolveString(key) {
  const resolvedKey = resolveKey(key);
  let str = _i18n._strings[resolvedKey];
  if (str === undefined || str === null) {
    str = _CRITICAL_FALLBACK_STRINGS[_i18n._lang]?.[resolvedKey]
      ?? _CRITICAL_FALLBACK_STRINGS.en[resolvedKey]
      ?? _CRITICAL_FALLBACK_STRINGS[_i18n._lang]?.[key]
      ?? _CRITICAL_FALLBACK_STRINGS.en[key]
      ?? humanizeMissingKey(key);
  }
  return str;
}

function humanizeMissingKey(key) {
  return String(key || "")
    .split(".")
    .pop()
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase()) || "";
}

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
    const resp = await fetch(`/static/locales/${lang}.json?v=20260422m`, { cache: "no-store" });
    if (!resp.ok) {
      console.warn(`[i18n] Locale '${lang}' not found, falling back to 'en'.`);
      if (lang !== "en") return loadLocale("en");
      return;
    }
    _i18n._strings = await resp.json();
    if (lang === "de") {
      _i18n._strings["composer.upload_file_desc"] = "Lade ein PDF, Word-Dokument, eine Tabelle, einen Mail-Export oder Notizen hoch";
      _i18n._strings["composer.from_documents_desc"] = "Waehle etwas aus, das du schon hochgeladen hast";
      _i18n._strings["upload.validation.count"] = "Du kannst auf einmal bis zu ${max} Dateien hochladen.";
      _i18n._strings["empty.heading_alt_1"] = "Bereit, wenn du es bist.";
      _i18n._strings["empty.heading_alt_2"] = "Womit legen wir los?";
      _i18n._strings["empty.heading_alt_3"] = "Lass uns etwas daraus machen.";
    }
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
  let str = resolveString(key);
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
