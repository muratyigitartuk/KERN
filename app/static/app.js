import { elements } from "/static/js/dashboard-dom.js";
import { bindDashboardEvents } from "/static/js/dashboard-events.js";
import { createDashboardRenderer } from "/static/js/dashboard-renderer.js";
import { createModalController } from "/static/js/modal-controller.js";
import { createSocketClient } from "/static/js/socket-client.js";
import { createThemeController } from "/static/js/theme-controller.js";
import { createWorkbenchController } from "/static/js/workbench.js";
import { applyText, loadLocale, t } from "/static/js/i18n.js";
import { bootstrapAdminAuthToken, withAdminToken } from "/static/js/utils.js";

const savedLang = localStorage.getItem("kern.ui.language") || "en";
await loadLocale(savedLang);
bootstrapAdminAuthToken();

function setText(target, key) {
  const element = typeof target === "string" ? document.querySelector(target) : target;
  if (element) {
    element.textContent = t(key);
  }
}

function setAttr(target, attr, key) {
  const element = typeof target === "string" ? document.querySelector(target) : target;
  if (element) {
    element.setAttribute(attr, t(key));
  }
}

function setValue(target, value) {
  const element = typeof target === "string" ? document.querySelector(target) : target;
  if (element) {
    element.textContent = value;
  }
}

function setLeadingTextNode(element, value) {
  if (!element) return;
  const textNode = [...element.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
  if (textNode) {
    textNode.textContent = value;
  }
}

function setParentLeadText(elementId, key, selector = ".eyebrow") {
  const element = document.getElementById(elementId);
  const label = element?.closest(".inspector-block, .settings-row, .inspector-block__head, .toggle-row, .mini-stat")?.querySelector(selector);
  if (label) {
    label.textContent = t(key);
  }
}

function setRowLabel(elementId, key) {
  const element = document.getElementById(elementId);
  const label = element?.closest(".settings-row")?.querySelector("span");
  if (label) {
    label.textContent = t(key);
  }
}

function setMiniStatLabel(elementId, key) {
  const element = document.getElementById(elementId);
  const label = element?.closest(".mini-stat")?.querySelector("span");
  if (label) {
    label.textContent = t(key);
  }
}

function setToggleLabel(elementId, key) {
  const element = document.getElementById(elementId);
  const label = element?.closest(".toggle-row")?.querySelector("span");
  if (label) {
    label.textContent = t(key);
  }
}

function setPreviousGroupLabel(elementId, key) {
  const element = document.getElementById(elementId);
  let sibling = element?.closest(".inspector-block, .utility-panel")?.previousElementSibling;
  while (sibling && !sibling.classList.contains("utility-group-label")) {
    sibling = sibling.previousElementSibling;
  }
  if (sibling) {
    sibling.textContent = t(key);
  }
}

function setLabelTextForInput(inputId, key) {
  const input = document.getElementById(inputId);
  const label = input?.closest(".settings-form-group")?.querySelector(".settings-label");
  if (label) {
    label.textContent = t(key);
  }
}

function applyStaticUiTranslations() {
  applyText();
  document.title = t("app.title");
  document.querySelector('meta[name="description"]')?.setAttribute("content", t("app.description"));

  setAttr("#conversationSearchModal .settings-modal__dialog", "aria-label", "nav.search_conversation");
  setAttr("#closeConversationSearch", "aria-label", "search.close");
  setAttr("#utilityModal .settings-modal__dialog", "aria-label", "utility.controls");
  setAttr("#closeUtilityModal", "aria-label", "utility.close");
  setAttr(".settings-modal__nav[role='tablist']", "aria-label", "utility.sections");
  setAttr("#alertBadge", "aria-label", "tabs.alerts_badge");
  setAttr("#closeSettings", "aria-label", "settings.close");
  setAttr("#settingsModal .settings-modal__nav", "aria-label", "settings.sections");
  setAttr("#themeModeGroup", "aria-label", "settings.theme_mode");
  setAttr("#settingsLanguageMenu", "aria-label", "settings.language");
  setAttr("#passageModal .settings-modal__dialog", "aria-label", "passage.aria");
  setAttr("#closePassageModal", "aria-label", "passage.close");
  setAttr("#uploadNoticeModal .settings-modal__dialog", "aria-label", "upload_notice.aria");

  setText("[data-tab='workspace'] .utility-tab__label", "tabs.workspace");
  setText("[data-tab='admin'] .utility-tab__label", "tabs.admin");
  setText("[data-tab='compliance'] .utility-tab__label", "tabs.compliance");
  setText("[data-tab='intelligence'] .utility-tab__label", "tabs.intelligence");
  setText("[data-tab='evidence'] .utility-tab__label", "tabs.evidence");
  setAttr("[data-tab='workspace']", "title", "tabs.workspace");
  setAttr("[data-tab='workspace']", "aria-label", "tabs.workspace");
  setAttr("[data-tab='admin']", "title", "tabs.admin");
  setAttr("[data-tab='admin']", "aria-label", "tabs.admin");
  setAttr("[data-tab='compliance']", "title", "tabs.compliance");
  setAttr("[data-tab='compliance']", "aria-label", "tabs.compliance");
  setAttr("[data-tab='intelligence']", "title", "tabs.intelligence");
  setAttr("[data-tab='intelligence']", "aria-label", "tabs.intelligence");
  setAttr("[data-tab='evidence']", "title", "tabs.evidence");
  setAttr("[data-tab='evidence']", "aria-label", "tabs.evidence");

  setParentLeadText("knowledgeBackend", "knowledge.title");
  setText("#knowledgeBackend", "knowledge.backend_lexical");
  setText("#knowledgeState", "knowledge.ready");
  setAttr("#knowledgeQuery", "placeholder", "knowledge.search_placeholder");
  setText("#knowledgeSearchButton", "knowledge.search");
  setAttr("#conversationSearchInput", "placeholder", "search.placeholder");
  setAttr("#composerKbSearch", "placeholder", "composer.search_docs");

  setParentLeadText("briefDate", "brief.title");
  setText("#briefDate", "brief.not_generated");
  setText("#focusText", "brief.default_focus");
  setText("#musicText", "brief.no_suggestion");
  setParentLeadText("eventsList", "sections.events");
  setParentLeadText("tasksList", "sections.tasks");
  setParentLeadText("remindersList", "sections.reminders");
  setParentLeadText("contextList", "sections.context");
  setParentLeadText("proactiveReason", "sections.suggestions");
  setText("#proactiveReason", "proactive.no_prompt");
  setText("#proactiveText", "proactive.quiet");

  setPreviousGroupLabel("planList", "plan.group");
  setParentLeadText("planList", "plan.current");
  setParentLeadText("receiptList", "plan.recently_done");
  setParentLeadText("capabilityList", "plan.available_tools");
  setPreviousGroupLabel("kgSearchInput", "kg.group");
  setParentLeadText("kgSearchInput", "kg.title");
  setAttr("#kgSearchInput", "placeholder", "kg.search_placeholder");
  setText("#kgSearchButton", "kg.search");
  setText("#kgBuildButton", "kg.build");
  setText("#kgStatus", "kg.empty");
  setParentLeadText("kgResultsList", "kg.entities");
  setPreviousGroupLabel("memorySearchInput", "memory.group");
  setParentLeadText("memorySearchInput", "memory.title");
  setAttr("#memorySearchInput", "placeholder", "memory.search_placeholder");
  setAttr("#memoryDateFrom", "title", "memory.from_date");
  setAttr("#memoryDateTo", "title", "memory.to_date");
  setText("#memorySearchButton", "memory.search");
  setParentLeadText("memoryTimeline", "memory.timeline");
  setParentLeadText("memoryResultsList", "memory.matching_turns");

  setPreviousGroupLabel("documentsList", "docs.group");
  setParentLeadText("documentsList", "docs.title");
  setLeadingTextNode(document.getElementById("bulkFileInput")?.parentElement, t("docs.upload"));
  setAttr("#dropZone", "aria-label", "docs.drop_label");
  setText("#dropZone .drop-zone__hint", "docs.drop_zone");
  setText("#uploadProgressLabel", "docs.uploading");
  setParentLeadText("businessDocsList", "docs.business_title");
  setPreviousGroupLabel("mailboxList", "email.group");
  setParentLeadText("mailboxList", "email.mailbox");
  setText("#syncMailboxButton", "email.sync");
  setParentLeadText("emailAccountsList", "email.accounts");
  setParentLeadText("draftsList", "email.drafts");
  setParentLeadText("emailSuggestionsList", "email.suggestions");
  setPreviousGroupLabel("meetingsList", "meetings.group");
  setParentLeadText("meetingsList", "meetings.title");
  setParentLeadText("meetingReviewsList", "meetings.summaries");

  setToggleLabel("localModeToggle", "ops.local_mode");
  setPreviousGroupLabel("deviceProfileMeta", "ops.system_health");
  setParentLeadText("deviceProfileMeta", "ops.system_status");
  setMiniStatLabel("voiceBackendName", "ops.voice");
  setText("#voiceBackendName", "ops.voice_backend");
  setText("#voiceBackendStatus", "ops.audio_waiting");
  setMiniStatLabel("systemProfileName", "ops.profile");
  setText("#systemProfileState", "ops.unlocked");
  setMiniStatLabel("systemMemoryScope", "ops.memory_scope");
  setMiniStatLabel("checkMemory", "ops.memory");
  setMiniStatLabel("checkCognition", "ops.cognition");
  setParentLeadText("jobsList", "ops.running_tasks");
  setParentLeadText("auditList", "ops.security_log");
  setPreviousGroupLabel("backupTargetsList", "backup.group");
  setParentLeadText("backupTargetsList", "backup.locations");
  setParentLeadText("backupFilesList", "backup.saved");
  setParentLeadText("syncTargetsList", "backup.sync_destinations");
  setParentLeadText("recoveryList", "backup.recovery");
  setPreviousGroupLabel("auditNetworkStatus", "audit.title");
  setParentLeadText("auditNetworkStatus", "network.title");
  setText("#auditNetworkStatus", "status.checking");
  setText("#auditNetworkDetail", "network.monitoring");
  setParentLeadText("auditLogList", "audit.title");
  setText("#auditCategoryFilter option[value='']", "audit.all_categories");
  setText("#auditCategoryFilter option[value='security']", "audit.security");
  setText("#auditCategoryFilter option[value='backup']", "audit.backup");
  setText("#auditCategoryFilter option[value='runtime']", "audit.runtime");
  setText("#auditCategoryFilter option[value='documents']", "audit.documents");
  setText("#auditCategoryFilter option[value='audit']", "audit.audit");
  setText("#auditCategoryFilter option[value='network']", "audit.network");
  setText("#auditCategoryFilter option[value='scheduler']", "audit.scheduler");
  setText("#exportAuditButton", "audit.export_json");
  setParentLeadText("domainNotesList", "settings.domain_notes");
  setPreviousGroupLabel("proactiveAlertsList", "schedules.group");
  setParentLeadText("proactiveAlertsList", "schedules.alerts");
  setText("#dismissAllAlertsButton", "schedules.dismiss_all");
  setParentLeadText("scheduleList", "schedules.title");
  setText("#addScheduleButton", "schedules.add");
  setText("#addScheduleForm > .eyebrow", "schedules.new");
  setLabelTextForInput("scheduleTitle", "schedules.form_title");
  setAttr("#scheduleTitle", "placeholder", "schedules.form_title_placeholder");
  setLabelTextForInput("scheduleFrequency", "schedules.form_frequency");
  setText("#scheduleFrequency option[value='0 9 * * *']", "schedules.freq_daily");
  setText("#scheduleFrequency option[value='0 9 * * 1']", "schedules.freq_weekly");
  setText("#scheduleFrequency option[value='0 9 1 * *']", "schedules.freq_monthly");
  setText("#scheduleFrequency option[value='custom']", "schedules.freq_custom");
  setAttr("#scheduleCron", "placeholder", "schedules.cron_placeholder");
  setLabelTextForInput("scheduleActionType", "schedules.form_action");
  setText("#scheduleActionType option[value='custom_prompt']", "schedules.action_prompt");
  setText("#scheduleActionType option[value='summarize_emails']", "schedules.action_email");
  setText("#scheduleActionType option[value='generate_report']", "schedules.action_report");
  setAttr("#scheduleActionPayload", "placeholder", "schedules.action_placeholder");
  setText("#saveScheduleButton", "schedules.save");
  setText("#cancelScheduleButton", "schedules.cancel");

  document.querySelector("[data-settings-section='appearance']")?.setAttribute("data-settings-title", t("settings.appearance"));
  document.querySelector("[data-settings-section='profile']")?.setAttribute("data-settings-title", t("settings.profile"));
  document.querySelector("[data-settings-section='model']")?.setAttribute("data-settings-title", t("settings.ai_models"));
  document.querySelector("[data-settings-section='domains']")?.setAttribute("data-settings-title", t("settings.domains"));
  setText("[data-settings-section-nav='appearance'] span", "settings.appearance");
  setText("[data-settings-section-nav='profile'] span", "settings.profile");
  setText("[data-settings-section-nav='model'] span", "settings.ai_models");
  setText("[data-settings-section-nav='domains'] span", "settings.domains");
  setText("#settingsModalTitle", "settings.general");
  setRowLabel("themeModeGroup", "settings.theme_mode");
  setText("[data-theme-mode='system']", "settings.theme_system");
  setText("[data-theme-mode='light']", "settings.theme_light");
  setText("[data-theme-mode='dark']", "settings.theme_dark");
  setRowLabel("settingsAppearancePreference", "settings.theme_pref");
  setRowLabel("settingsAppearanceActiveTheme", "settings.active_theme");
  setRowLabel("settingsThemeNote", "settings.theme_note");
  setRowLabel("settingsLanguageButton", "settings.language");
  setText("#settingsLanguageLabel", savedLang === "de" ? "settings.language_german" : "settings.language_english");
  setText("[data-language-option='en']", "settings.language_english");
  setText("[data-language-option='de']", "settings.language_german");

  setRowLabel("settingsProfileName", "settings.active_profile");
  setRowLabel("settingsProfileState", "settings.session_state");
  setRowLabel("settingsMemoryScope", "settings.data_scope");
  setRowLabel("settingsDocumentsRoot", "settings.docs_root");
  setRowLabel("settingsArchiveRoot", "settings.archive_root");
  setRowLabel("settingsBackupRoot", "settings.backup_root");
  setRowLabel("settingsReadinessStatus", "settings.readiness");
  setText("#settingsLicenseCard .eyebrow", "settings.license");
  setText("#settingsImportLicense", "settings.import_license");
  setText("#settingsRefreshLicense", "settings.recheck_license");
  setRowLabel("settingsRerunReadiness", "settings.readiness_action");
  setRowLabel("settingsAuditState", "settings.audit_label");
  setRowLabel("settingsAuditChain", "settings.security_log");
  setRowLabel("settingsDbEncryption", "settings.encryption");
  setRowLabel("settingsKeyVersion", "settings.key_version");
  setRowLabel("settingsArtifactEncryption", "settings.file_encryption");
  setRowLabel("settingsSessionPin", "settings.session_pin");
  setAttr("#settingsSessionPin", "placeholder", "settings.pin_placeholder");
  setText("#settingsSavePin", "settings.save_pin");
  setText("#settingsLockProfile", "settings.lock_profile");
  setText("#settingsUnlockProfile", "settings.unlock_profile");
  setRowLabel("settingsBackupPassword", "settings.encrypted_backup");
  setAttr("#settingsBackupPassword", "placeholder", "settings.backup_password");
  setText("#settingsCreateBackup", "settings.create_backup");
  setRowLabel("settingsSupportBundlePath", "settings.support_bundle");
  setText("#settingsExportSupportBundle", "settings.export_support_bundle");
  setRowLabel("settingsSupportBundleLastExport", "settings.last_support_export");
  setRowLabel("settingsUpdateChannel", "settings.update_channel");
  setText("#settingsUpdateCard .eyebrow", "settings.update_policy");
  setText("#settingsDataLifecycleNote", "settings.data_lifecycle_note");

  setRowLabel("settingsModelName", "settings.ai_engine");
  setRowLabel("settingsModelType", "settings.type");
  setRowLabel("settingsModelBackend", "settings.backend");
  setRowLabel("settingsModelMode", "settings.mode");
  setRowLabel("settingsModelPath", "settings.model_path");
  setRowLabel("settingsFastModelPath", "settings.fast_model");
  setRowLabel("settingsDeepModelPath", "settings.deep_model");
  setRowLabel("settingsEmbedModel", "settings.embed_model");
  setRowLabel("settingsRetrievalBackend", "settings.search_method");
  setRowLabel("settingsRetrievalHealth", "settings.search_index");
  setRowLabel("settingsVoiceBackend", "settings.voice_output");
  setRowLabel("settingsCloudMode", "settings.data_mode");
  setText("#settingsModelStoryPill", "status.checking");
  setText("#settingsModelStoryTitle", "settings.model_story_initial_title");
  setText("#settingsModelStoryText", "settings.model_story_initial_body");

  setRowLabel("settingsDocumentCount", "settings.indexed_docs");
  setRowLabel("settingsEmailAccounts", "settings.email_accounts");
  setRowLabel("settingsDraftCount", "settings.email_drafts");
  setRowLabel("settingsMeetingCount", "settings.meetings_label");
  setRowLabel("settingsBusinessCount", "settings.business_docs");
  setRowLabel("settingsSyncCount", "settings.sync_targets");

  setAttr("#closeUploadNotice", "aria-label", "upload_notice.close");
}

applyStaticUiTranslations();

const themeController = createThemeController();

const passageController = createModalController({
  modal: elements.passageModal,
  dialog: elements.passageModal.querySelector(".passage-modal__dialog"),
  backdrop: elements.passageBackdrop,
  closeButton: elements.closePassageModal,
});

const settingsController = createModalController({
  modal: elements.settingsModal,
  dialog: elements.settingsModal.querySelector(".settings-modal__dialog"),
  backdrop: elements.settingsBackdrop,
  closeButton: elements.closeSettings,
});

const utilityController = createModalController({
  modal: elements.utilityModal,
  dialog: elements.utilityModal.querySelector(".settings-modal__dialog"),
  backdrop: elements.utilityBackdrop,
  closeButton: elements.closeUtilityModal,
});

const conversationSearchController = createModalController({
  modal: elements.conversationSearchModal,
  dialog: elements.conversationSearchModal.querySelector(".settings-modal__dialog"),
  backdrop: elements.conversationSearchBackdrop,
  closeButton: elements.closeConversationSearch,
});

const uploadNoticeController = createModalController({
  modal: elements.uploadNoticeModal,
  dialog: elements.uploadNoticeModal.querySelector(".settings-modal__dialog"),
  backdrop: elements.uploadNoticeBackdrop,
  closeButton: elements.closeUploadNotice,
});

const renderer = createDashboardRenderer({
  elements,
  send,
  themeController,
  passageController,
});

const socketClient = createSocketClient({
  url: withAdminToken(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`),
  onOpen: renderer.updateClock,
  onMessage(message) {
    if (message.type === "snapshot") {
      renderer.scheduleSnapshotRender(message.payload);
    } else if (message.type === "llm_token") {
      renderer.appendLlmToken(message.payload?.token || "");
    } else if (message.type === "llm_done") {
      renderer.finalizeLlmStream(message.payload?.rag || false);
    } else if (message.type === "rag_sources") {
      renderer.renderRagSources(message.payload);
    } else if (message.type === "knowledge_graph_data") {
      renderer.renderKnowledgeGraph(message.graph || {});
    } else if (message.type === "knowledge_graph_search") {
      renderer.renderKnowledgeGraphSearch(message.entities || []);
    } else if (message.type === "memory_search_result") {
      renderer.renderMemorySearchResults(message.hits || []);
    } else if (message.type === "audit_export") {
      const blob = new Blob([message.payload], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `kern-audit-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }
  },
  onStateChange: renderer.updateConnectionState,
});

function send(payload) {
  const sent = socketClient.send(payload);
  if (!sent) {
    elements.statusText.textContent = t("app.command_not_sent");
  }
  return sent;
}

const workbenchController = createWorkbenchController({ renderer });

const baseActivateUtilityTab = renderer.activateUtilityTab;
renderer.activateUtilityTab = (tabName) => {
  baseActivateUtilityTab(tabName);
  workbenchController.onTabActivated(tabName).catch((error) => {
    console.error("[KERN] workbench tab activation failed:", error);
  });
};

bindDashboardEvents({
  elements,
  renderer,
  send,
  settingsController,
  utilityController,
  conversationSearchController,
  uploadNoticeController,
  themeController,
});

await workbenchController.init();

renderer.updateClock();
window.setInterval(renderer.updateClock, 1000);
renderer.activateUtilityTab("workspace");
renderer.activateSettingsSection("appearance", { behavior: "auto", scroll: false });
renderer.applySidebarCollapsed(localStorage.getItem(renderer.getSidebarCollapsedKey()) === "1");
renderer.autoResizeCommandInput();
renderer.syncConversationState([]);
renderer.renderThemeState();
themeController.subscribe(() => {
  renderer.renderThemeState();
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register(withAdminToken("/static/service-worker.js")).catch((err) => { console.error("[KERN] SW registration failed:", err); });
  });
}
