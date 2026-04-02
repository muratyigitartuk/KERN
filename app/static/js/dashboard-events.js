import { t, loadLocale, getCurrentLang } from "/static/js/i18n.js";
import { secureFetch } from "/static/js/utils.js";

const UPLOAD_MAX_FILE_MB = 50;
const UPLOAD_ALLOWED_EXTENSIONS = new Set([
  ".txt", ".md", ".pdf", ".csv", ".xlsx", ".xls",
  ".doc", ".docx", ".eml", ".json", ".html", ".htm", ".xml", ".rtf",
]);
const FILENAME_UNSAFE_RE = /[<>:"|?*\x00-\x1f]/;

function isFileTransfer(event) {
  const types = event?.dataTransfer?.types;
  if (!types) {
    return false;
  }
  if (typeof types.includes === "function") {
    return types.includes("Files");
  }
  if (typeof types.contains === "function") {
    return types.contains("Files");
  }
  return false;
}

function validateUploadFiles(files) {
  for (const file of files) {
    if (!file.name || file.size === 0) {
      return t("upload.validation.empty");
    }
    const ext = (file.name.includes(".") ? "." + file.name.split(".").pop().toLowerCase() : "");
    if (!ext || !UPLOAD_ALLOWED_EXTENSIONS.has(ext)) {
      return t("upload.validation.type", { ext: ext || t("upload.validation.none"), name: file.name });
    }
    if (file.size > UPLOAD_MAX_FILE_MB * 1024 * 1024) {
      return t("upload.validation.size", { max: UPLOAD_MAX_FILE_MB, name: file.name });
    }
    if (FILENAME_UNSAFE_RE.test(file.name)) {
      return t("upload.validation.filename", { name: file.name });
    }
  }
  return null;
}

export function bindDashboardEvents({
  elements,
  renderer,
  send,
  settingsController,
  utilityController,
  conversationSearchController,
  uploadNoticeController,
  themeController,
}) {
  const UPLOAD_NOTICE_STORAGE_PREFIX = "kern.upload.notice.dismissed";
  let dismissedComposerUploadKeys = new Set();
  let composerAssistHideTimer = null;

  function currentSnapshot() {
    return renderer.getCurrentSnapshot?.() || null;
  }

  function uploadNoticeStorageKey() {
    const profileSlug = currentSnapshot()?.profile_slug || "default";
    return `${UPLOAD_NOTICE_STORAGE_PREFIX}.${profileSlug}`;
  }

  function uploadNoticeDismissed() {
    return localStorage.getItem(uploadNoticeStorageKey()) === "1";
  }

  function persistUploadNoticeDismissed() {
    localStorage.setItem(uploadNoticeStorageKey(), "1");
  }

  function shouldShowUploadNotice() {
    return false;
  }

  function applyUploadNoticeCopy() {
    if (!elements.uploadNoticeTitle) return;
    elements.uploadNoticeEyebrow.textContent = t("upload_notice.eyebrow");
    elements.uploadNoticeTitle.textContent = t("upload_notice.title");
    elements.uploadNoticeLead.textContent = t("upload_notice.lead");
    elements.uploadNoticeExtractionLabel.textContent = t("upload_notice.extraction_label");
    elements.uploadNoticeExtractionText.textContent = t("upload_notice.extraction_text");
    elements.uploadNoticePrivacyLabel.textContent = t("upload_notice.privacy_label");
    elements.uploadNoticePrivacyText.textContent = t("upload_notice.privacy_text");
    elements.uploadNoticeDismissText.textContent = t("upload_notice.dismiss");
    elements.uploadNoticeCancel.textContent = t("upload_notice.cancel");
    elements.uploadNoticeContinue.textContent = t("upload_notice.continue");
    elements.closeUploadNotice.setAttribute("aria-label", t("upload_notice.close"));
  }

  applyUploadNoticeCopy();

  function maybeShowUploadNotice() {
    if (!shouldShowUploadNotice()) {
      return Promise.resolve(true);
    }
    applyUploadNoticeCopy();
    elements.uploadNoticeDismiss.checked = false;

    return new Promise((resolve) => {
      let settled = false;
      let proceeding = false;

      const finish = (accepted) => {
        if (settled) return;
        settled = true;
        uploadNoticeController.setOnClose(null);
        elements.uploadNoticeContinue.removeEventListener("click", handleContinue);
        elements.uploadNoticeCancel.removeEventListener("click", handleCancel);
        if (accepted && elements.uploadNoticeDismiss.checked) {
          persistUploadNoticeDismissed();
        }
        resolve(accepted);
      };

      const handleContinue = () => {
        proceeding = true;
        uploadNoticeController.close();
        finish(true);
      };

      const handleCancel = () => {
        uploadNoticeController.close();
        finish(false);
      };

      uploadNoticeController.setOnClose(() => {
        if (proceeding) {
          return;
        }
        finish(false);
      });
      elements.uploadNoticeContinue.addEventListener("click", handleContinue);
      elements.uploadNoticeCancel.addEventListener("click", handleCancel);
      uploadNoticeController.open();
    });
  }

  function stagePrompt(prompt) {
    elements.commandInput.value = prompt;
    renderer.setConversationPrimed(true);
    renderer.autoResizeCommandInput();
    renderer.syncConversationState();
    requestAnimationFrame(() => {
      elements.commandInput.focus();
      elements.commandInput.setSelectionRange(elements.commandInput.value.length, elements.commandInput.value.length);
      elements.commandInput.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }

  function saveOnboarding(settings) {
    return send({ type: "update_settings", settings });
  }

  function buildGroundedDraftPrompt(snapshot = currentSnapshot()) {
    const docs = snapshot?.recent_documents || [];
    if (docs.length) {
      const title = docs[0].title || docs[0].source_id || t("docs.untitled");
      return t("onboarding.workflow_prompt_one", { title });
    }
    return t("onboarding.workflow_prompt_none");
  }

  function buildSampleDraftPrompt() {
    return t("onboarding.sample_prompt");
  }

  function clearComposerAssist() {
    setComposerAssist("");
  }

  function composerAssistLabel(tone = "info") {
    if (tone === "error") {
      return t("composer.status_error");
    }
    if (tone === "success") {
      return t("composer.status_success");
    }
    if (tone === "warning") {
      return t("composer.status_warning");
    }
    return t("composer.status_info");
  }

  function optimisticOnboardingState(step, snapshot = currentSnapshot()) {
    const hasDocs = Boolean(snapshot?.recent_documents?.length);
    if (step === "model") {
      return {
        active: true,
        current_step: "model",
        title: t("onboarding.optimistic_model_title"),
        body: t("onboarding.optimistic_model_body"),
        primary_action: t("onboarding.optimistic_model_primary"),
        secondary_action: "",
      };
    }
    if (step === "workflow") {
      return {
        active: true,
        current_step: "workflow",
        title: t("onboarding.optimistic_workflow_title"),
        body: hasDocs ? t("onboarding.optimistic_workflow_body_ready") : t("onboarding.optimistic_workflow_body_upload"),
        primary_action: hasDocs
          ? t("onboarding.optimistic_workflow_primary_ready")
          : t("onboarding.optimistic_workflow_primary_upload"),
        secondary_action: t("onboarding.optimistic_workflow_secondary"),
      };
    }
    if (step === "sample") {
      return {
        active: true,
        current_step: "sample",
        title: t("onboarding.optimistic_sample_title"),
        body: t("onboarding.optimistic_sample_body"),
        primary_action: t("onboarding.optimistic_sample_primary"),
        secondary_action: t("onboarding.optimistic_sample_secondary"),
      };
    }
    return null;
  }

  function setOnboardingPending(options) {
    renderer.setOnboardingOptimisticState?.({
      pending: true,
      ...options,
    });
  }

  function hideOnboardingLocally() {
    renderer.setOnboardingOptimisticState?.({
      pending: false,
      untilInactive: true,
      override: { active: false },
    });
  }

  let workspaceDragDepth = 0;
  let workspaceDropHideTimer = null;

  function clearWorkspaceDropHideTimer() {
    if (workspaceDropHideTimer) {
      clearTimeout(workspaceDropHideTimer);
      workspaceDropHideTimer = null;
    }
  }

  function setWorkspaceDropProgress(visible, { label = "", percent = 0 } = {}) {
    if (!elements.workspaceDropProgress || !elements.workspaceDropProgressLabel || !elements.workspaceDropProgressFill) {
      return;
    }
    elements.workspaceDropProgress.classList.toggle("hidden", !visible);
    if (!visible) {
      elements.workspaceDropProgressLabel.textContent = "";
      elements.workspaceDropProgressFill.style.width = "0%";
      return;
    }
    elements.workspaceDropProgressLabel.textContent = label;
    elements.workspaceDropProgressFill.style.width = `${percent}%`;
  }

  function renderWorkspaceDropOverlay(state, { count = 0, message = "" } = {}) {
    if (!elements.workspaceDropPanel || !elements.workspaceDropTitle || !elements.workspaceDropBody) {
      return;
    }

    let titleKey = "dropzone.ready_title";
    let bodyText = t("dropzone.ready_body");

    if (state === "blocked") {
      titleKey = "dropzone.blocked_title";
      bodyText = t("dropzone.blocked_body");
    } else if (state === "uploading") {
      titleKey = "dropzone.uploading_title";
      bodyText = t("dropzone.uploading_body", { count });
    } else if (state === "success") {
      titleKey = "dropzone.success_title";
      bodyText = t("dropzone.success_body", { count });
    } else if (state === "error") {
      titleKey = "dropzone.error_title";
      bodyText = message || t("dropzone.error_body");
    }

    elements.workspaceDropPanel.dataset.state = state;
    elements.workspaceDropTitle.textContent = t(titleKey, { count });
    elements.workspaceDropBody.textContent = bodyText;
    setWorkspaceDropProgress(state === "uploading", {
      label: t("dropzone.progress", { count }),
      percent: state === "uploading" ? 36 : 0,
    });
  }

  function showWorkspaceDropOverlay(state = "ready", options = {}) {
    if (!elements.workspaceDropOverlay || !elements.workspaceShell) {
      return;
    }
    clearWorkspaceDropHideTimer();
    renderWorkspaceDropOverlay(state, options);
    elements.workspaceDropOverlay.classList.remove("hidden");
    elements.workspaceDropOverlay.setAttribute("aria-hidden", "false");
    elements.workspaceShell.classList.add("is-workspace-drag-over");
  }

  function hideWorkspaceDropOverlay({ delayed = false } = {}) {
    if (!elements.workspaceDropOverlay || !elements.workspaceShell) {
      return;
    }
    clearWorkspaceDropHideTimer();
    const commitHide = () => {
      elements.workspaceShell.classList.remove("is-workspace-drag-over");
      elements.workspaceDropOverlay.classList.add("hidden");
      elements.workspaceDropOverlay.setAttribute("aria-hidden", "true");
      setWorkspaceDropProgress(false);
      renderWorkspaceDropOverlay("ready");
    };
    if (!delayed) {
      commitHide();
      return;
    }
    workspaceDropHideTimer = window.setTimeout(commitHide, 1200);
  }

  let lastUploadDocuments = [];
  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll("\"", "&quot;")
      .replaceAll("'", "&#39;");
  }

  function summarizeQueue(items = []) {
    return items.reduce((summary, item) => {
      const status = String(item?.status || "indexed");
      if (status === "indexed") {
        summary.indexed += 1;
      } else if (status === "duplicate") {
        summary.duplicates += 1;
      } else if (status === "pending") {
        summary.pending += 1;
      } else {
        summary.review += 1;
      }
      return summary;
    }, { indexed: 0, duplicates: 0, pending: 0, review: 0 });
  }

  function renderQueueStats(target, summary) {
    if (!target) {
      return;
    }
    const parts = [];
    if (summary.pending) {
      parts.push(`
        <span class="composer-upload-stat">
          <strong>${summary.pending}</strong>
          <span>${escapeHtml(t("upload.item_status_pending"))}</span>
        </span>
      `);
    }
    if (summary.indexed) {
      parts.push(`
        <span class="composer-upload-stat composer-upload-stat--indexed">
          <strong>${summary.indexed}</strong>
          <span>${escapeHtml(t("upload.item_status_indexed"))}</span>
        </span>
      `);
    }
    if (summary.duplicates) {
      parts.push(`
        <span class="composer-upload-stat composer-upload-stat--duplicate">
          <strong>${summary.duplicates}</strong>
          <span>${escapeHtml(t("upload.item_status_duplicate"))}</span>
        </span>
      `);
    }
    if (summary.review) {
      parts.push(`
        <span class="composer-upload-stat composer-upload-stat--review">
          <strong>${summary.review}</strong>
          <span>${escapeHtml(t("upload.queue_review"))}</span>
        </span>
      `);
    }
    target.innerHTML = parts.join("");
  }

  function renderOutcomeStats(target, summary) {
    if (!target) {
      return;
    }
    const parts = [];
    if (summary.indexed) {
      parts.push(`
        <span class="upload-outcome-stat upload-outcome-stat--indexed">
          <strong>${summary.indexed}</strong>
          <span>${escapeHtml(t("upload.item_status_indexed"))}</span>
        </span>
      `);
    }
    if (summary.duplicates) {
      parts.push(`
        <span class="upload-outcome-stat upload-outcome-stat--duplicate">
          <strong>${summary.duplicates}</strong>
          <span>${escapeHtml(t("upload.item_status_duplicate"))}</span>
        </span>
      `);
    }
    if (summary.review) {
      parts.push(`
        <span class="upload-outcome-stat upload-outcome-stat--review">
          <strong>${summary.review}</strong>
          <span>${escapeHtml(t("upload.queue_review"))}</span>
        </span>
      `);
    }
    target.innerHTML = parts.join("");
    target.classList.toggle("hidden", !parts.length);
  }

  function renderComposerUploads(items = []) {
    if (!elements.composerUploads || !elements.composerUploadsList) {
      return;
    }
    const rawEntries = items.filter(Boolean).map((item, index) => ({
      ...item,
      key: String(item.key || `${item.label || item.title || item.name || "file"}::${item.status || "indexed"}::${index}`),
    }));
    const visibleEntries = rawEntries.filter((item) => !dismissedComposerUploadKeys.has(item.key));
    const hasEntries = visibleEntries.length > 0;
    if (rawEntries.length && !hasEntries) {
      dismissedComposerUploadKeys = new Set();
    }
    const entries = rawEntries.filter((item) => !dismissedComposerUploadKeys.has(item.key));
    if (!entries.length) {
      elements.composerUploads.classList.add("hidden");
      elements.composerUploadsList.innerHTML = "";
      return;
    }
    elements.composerUploads.classList.remove("hidden");
    elements.composerUploadsList.innerHTML = entries.map((item) => {
      const label = escapeHtml(item.label || item.title || item.name || t("docs.untitled"));
      const detail = escapeHtml(item.detail || "");
      const status = escapeHtml(item.status || "indexed");
      const ext = escapeHtml(String(item.extension || item.label || "")
        .split(".")
        .pop()
        .replace(/[^a-z0-9]/gi, "")
        .toUpperCase() || t(`upload.item_status_${status}`));
      return `
          <span class="composer-upload-chip composer-upload-chip--${status}" title="${detail || label}">
            <span class="composer-upload-chip__badge" aria-hidden="true"></span>
            <span class="composer-upload-chip__content">
              <span class="composer-upload-chip__label">${label}</span>
              <span class="composer-upload-chip__detail">${ext}</span>
            </span>
            <button type="button" class="composer-upload-chip__remove" data-remove-upload-key="${escapeHtml(item.key)}" aria-label="${escapeHtml(t("composer.remove_attachment"))}">&times;</button>
          </span>
        `;
      }).join("");
    }

    function renderPendingUploads(files) {
      dismissedComposerUploadKeys = new Set();
      const entries = files.map((file) => ({
        label: file.name,
        status: "pending",
        detail: t("upload.item_pending_detail"),
        extension: file.name,
        key: `pending::${file.name}::${file.size}`,
      }));
      renderComposerUploads(entries);
    }

  function uploadMetaText({ indexed = 0, duplicates = 0, rejected = 0, failed = 0 } = {}) {
    if (indexed === 0 && duplicates > 0 && rejected === 0 && failed === 0) {
      return t("composer.attachments_duplicates_only", { count: duplicates });
    }
    if (indexed > 0 && duplicates === 0 && rejected === 0 && failed === 0) {
      return t("composer.attachments_indexed_only", { count: indexed });
    }
    return t("composer.attachments_mixed", {
      indexed,
      duplicates,
      rejected: rejected + failed,
    });
  }

  function describeUploadFailure(payload = {}, fallbackMessage = "") {
    const items = Array.isArray(payload?.items) ? payload.items : [];
    const firstProblem = items.find((item) => ["failed", "rejected"].includes(String(item?.status || "").toLowerCase())) || null;
    const itemName = String(firstProblem?.name || "");
    const detail = String(firstProblem?.detail || fallbackMessage || t("upload.error_body")).trim();
    const isPdf = /\.pdf$/i.test(itemName);
    const looksUnreadable = /extract|readable text|ocr|pymupdf|paddle|pdf/i.test(detail);
    if (isPdf && looksUnreadable) {
      return {
        title: t("upload.error_unreadable_pdf_title"),
        body: t("upload.error_unreadable_pdf_body"),
      };
    }
    return {
      title: t("upload.error_failed_item_title"),
      body: detail || t("upload.error_body"),
    };
  }

    function hideUploadOutcome() {
      if (!elements.uploadOutcomeCard) return;
      elements.uploadOutcomeCard.classList.add("hidden");
      dismissedComposerUploadKeys = new Set();
      renderComposerUploads([]);
    }

  function buildUploadPrompt(kind) {
    const docs = lastUploadDocuments.filter(Boolean);
    if (!docs.length) {
      return kind === "ask"
        ? t("upload.ask_fallback")
        : t("upload.summary_fallback");
    }
    if (kind === "compare" && docs.length >= 2) {
      return t("upload.compare_prompt");
    }
    if (docs.length === 1) {
      const title = docs[0].title || docs[0].category || t("docs.untitled");
      return kind === "ask"
        ? t("upload.ask_one", { title })
        : t("upload.summary_one", { title });
    }
    return kind === "ask"
      ? t("upload.ask_many", { count: docs.length })
      : t("upload.summary_many", { count: docs.length });
  }

  function renderUploadOutcome({
    tone = "success",
    pill,
    title,
    meta,
    body,
    documents = [],
    items = [],
    queueMeta = "",
    stats = null,
  }) {
    if (!elements.uploadOutcomeCard) return;
    lastUploadDocuments = documents;
    elements.uploadOutcomeCard.dataset.tone = tone;
    elements.uploadOutcomePill.textContent = pill;
    elements.uploadOutcomeTitle.textContent = title;
    elements.uploadOutcomeMeta.textContent = meta || "";
    renderOutcomeStats(elements.uploadOutcomeStats, stats || summarizeQueue(items));
    elements.uploadOutcomeBody.textContent = body;
    elements.uploadOutcomePrimary.textContent = t("upload.action_summarize");
    elements.uploadOutcomeSecondary.textContent = t("upload.action_ask");
    elements.uploadOutcomeCompare.textContent = t("upload.action_compare");
    elements.uploadOutcomeCompare.classList.toggle("hidden", documents.length < 2);
    elements.uploadOutcomeCard.classList.remove("hidden");
    const documentsById = new Map(
      documents.filter((doc) => doc?.id).map((doc) => [doc.id, doc]),
    );
    const queueItems = items.length
      ? items.map((item) => {
          const mappedDocument = item.document?.id ? documentsById.get(item.document.id) : null;
          const ocrWarning = mappedDocument?.ocr_low_confidence
            ? t("upload.item_ocr_detail")
            : "";
            return {
              label: item.document?.title || item.name || t("docs.untitled"),
              status: item.status || "indexed",
              detail: ocrWarning || item.detail || "",
              extension: item.name || item.document?.title || "",
              key: item.document?.id ? `doc::${item.document.id}` : `item::${item.name || item.document?.title || ""}::${item.status || "indexed"}`,
            };
          })
        : documents.map((doc) => ({
            label: doc.title || doc.source_id || doc.category || t("docs.untitled"),
            status: "indexed",
            detail: doc.ocr_low_confidence ? t("upload.item_ocr_detail") : t("upload.item_indexed_detail"),
            extension: doc.title || doc.source_id || "",
            key: doc.id ? `doc::${doc.id}` : `doc::${doc.title || doc.source_id || ""}`,
          }));
      renderComposerUploads(queueItems);
    }

  function setComposerAssist(message, tone = "info") {
    if (!elements.composerAssist) return;
    if (composerAssistHideTimer) {
      window.clearTimeout(composerAssistHideTimer);
      composerAssistHideTimer = null;
    }
    if (!message) {
      if (elements.composerAssistSummary) {
        elements.composerAssistSummary.textContent = "";
      }
      if (elements.composerAssistBadge) {
        elements.composerAssistBadge.textContent = "";
      }
      elements.composerAssist.classList.add("hidden");
      elements.composerAssist.classList.remove("is-error");
      elements.composerAssist.classList.remove("is-success");
      elements.composerAssist.classList.remove("is-warning");
      return;
    }
    if (elements.composerAssistSummary) {
      elements.composerAssistSummary.textContent = message;
    }
    if (elements.composerAssistBadge) {
      elements.composerAssistBadge.textContent = composerAssistLabel(tone);
    }
    elements.composerAssist.classList.remove("hidden");
    elements.composerAssist.classList.toggle("is-error", tone === "error");
    elements.composerAssist.classList.toggle("is-success", tone === "success");
    elements.composerAssist.classList.toggle("is-warning", tone === "warning");
  }

  elements.composerAssistDismiss?.addEventListener("click", () => {
    clearComposerAssist();
  });

  elements.onboardingPrimaryAction?.addEventListener("click", () => {
    clearComposerAssist();
    const snapshot = currentSnapshot();
    const step = snapshot?.onboarding?.current_step || "storage";
    if (step === "storage") {
      setOnboardingPending({
        untilStep: "model",
        override: optimisticOnboardingState("model", snapshot),
      });
      saveOnboarding({ onboarding_storage_confirmed: true });
      return;
    }
    if (step === "model") {
      setOnboardingPending({
        untilStep: "workflow",
        override: optimisticOnboardingState("workflow", snapshot),
      });
      saveOnboarding({ onboarding_model_choice: "recommended_local" });
      return;
    }
    if (step === "sample") {
      setOnboardingPending({
        untilStep: "workflow",
        untilInactive: true,
        override: optimisticOnboardingState("workflow", snapshot),
      });
      send({ type: "start_real_workspace" });
      return;
    }
    if (step !== "workflow") {
      return;
    }

    hideOnboardingLocally();
    saveOnboarding({
      onboarding_selected_path: "real_documents",
      onboarding_starter_workflow: "document_grounded_draft",
      onboarding_completed: true,
    });

    const docs = snapshot?.recent_documents || [];
    if (!docs.length) {
      elements.composerFileInput?.click();
      return;
    }

    const prompt = buildGroundedDraftPrompt(snapshot);
    if (!send({ type: "submit_text", text: prompt })) {
      stagePrompt(prompt);
      return;
    }
    renderer.setConversationPrimed(true);
  });

  elements.onboardingSecondaryAction?.addEventListener("click", () => {
    clearComposerAssist();
    const snapshot = currentSnapshot();
    const step = snapshot?.onboarding?.current_step;
    if (step === "sample") {
      hideOnboardingLocally();
      stagePrompt(buildSampleDraftPrompt());
      return;
    }
    if (step !== "workflow") {
      return;
    }
    setOnboardingPending({
      untilStep: "sample",
      override: optimisticOnboardingState("sample", snapshot),
    });
    send({ type: "start_sample_workspace" });
  });

  elements.onboardingDismiss?.addEventListener("click", () => {
    clearComposerAssist();
    const step = currentSnapshot()?.onboarding?.current_step;
    if (step === "sample") {
      setOnboardingPending({
        untilStep: "workflow",
        untilInactive: true,
        override: optimisticOnboardingState("workflow"),
      });
      send({ type: "start_real_workspace" });
      return;
    }
    hideOnboardingLocally();
    saveOnboarding({
      onboarding_starter_workflow: "document_grounded_draft",
      onboarding_completed: true,
    });
  });

  elements.onboardingBackdrop?.addEventListener("click", () => {
    elements.onboardingDismiss?.click();
  });

  function resetConversation() {
    if (!send({ type: "reset_conversation" })) {
      return;
    }
    hideUploadOutcome();
    renderer.setConversationPrimed(false);
    elements.commandInput.value = "";
    renderer.autoResizeCommandInput();
    if (elements.conversationSearchInput) {
      elements.conversationSearchInput.value = "";
    }
    renderer.syncConversationState([]);
  }

    elements.commandForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = elements.commandInput.value.trim();
    if (!text) return;
    if (!send({ type: "submit_text", text })) {
      return;
    }
    renderer.setConversationPrimed(true);
    elements.commandInput.value = "";
    renderer.autoResizeCommandInput();
    renderer.syncConversationState();
  });

  elements.confirmButton.addEventListener("click", () => {
    send({ type: "confirm_action" });
  });

  elements.cancelButton.addEventListener("click", () => {
    send({ type: "cancel_action" });
  });

  elements.uploadOutcomePrimary?.addEventListener("click", () => {
    const prompt = buildUploadPrompt("summarize");
    if (!send({ type: "submit_text", text: prompt })) {
      stagePrompt(prompt);
      setComposerAssist(t("upload.followup_staged"), "warning");
    } else {
      setComposerAssist(t("upload.followup_sent"), "success");
    }
  });

  elements.uploadOutcomeSecondary?.addEventListener("click", () => {
    stagePrompt(buildUploadPrompt("ask"));
    setComposerAssist(t("upload.followup_ready"), "success");
  });

  elements.uploadOutcomeCompare?.addEventListener("click", () => {
    const ids = lastUploadDocuments.map((doc) => doc?.id).filter(Boolean);
    if (ids.length < 2) {
      return;
    }
    if (!send({ type: "submit_text", text: `compare_documents ${JSON.stringify(ids)} :: ${t("docs.compare_default_query")}` })) {
      setComposerAssist(t("upload.compare_offline"), "error");
      return;
    }
    setComposerAssist(t("upload.compare_sent"), "success");
  });

  elements.promptButtons.forEach((button) => {
    button.addEventListener("click", () => {
      stagePrompt(button.dataset.prompt || button.textContent || "");
    });

    elements.composerUploadsList?.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-remove-upload-key]") : null;
      if (!button) {
        return;
      }
      const key = button.getAttribute("data-remove-upload-key");
      if (!key) {
        return;
      }
      dismissedComposerUploadKeys.add(key);
      const cards = [...elements.composerUploadsList.querySelectorAll("[data-remove-upload-key]")];
      const remaining = cards.filter((card) => card.getAttribute("data-remove-upload-key") !== key);
      button.closest(".composer-upload-chip")?.remove();
      if (!remaining.length) {
        elements.composerUploads.classList.add("hidden");
      }
    });
  });

  elements.newConversation.addEventListener("click", resetConversation);
  elements.sidebarHome?.addEventListener("click", resetConversation);

  elements.openConversationSearch?.addEventListener("click", () => {
    conversationSearchController.open();
    renderer.renderConversationSearch("");
    requestAnimationFrame(() => {
      elements.conversationSearchInput?.focus();
      elements.conversationSearchInput?.select();
    });
  });

  elements.conversationSearchInput?.addEventListener("input", () => {
    renderer.renderConversationSearch(elements.conversationSearchInput.value);
  });

  elements.conversationSearchResults?.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest(".search-result") : null;
    if (!button) {
      return;
    }
    const turnId = button.getAttribute("data-turn-id");
    conversationSearchController.close();
    requestAnimationFrame(() => {
      renderer.scrollToTurn(turnId);
    });
  });

  elements.sidebarToggle.addEventListener("click", () => {
    renderer.applySidebarCollapsed(!elements.workspaceShell.classList.contains("sidebar-collapsed"));
  });

  elements.openSettings.addEventListener("click", () => {
    settingsController.open();
    renderer.activateSettingsSection(
      [...elements.settingsSectionNavItems].find((item) => item.classList.contains("is-active"))?.dataset.settingsSectionNav || "appearance",
      { behavior: "auto" }
    );
  });

  elements.settingsLockProfile.addEventListener("click", () => {
    send({ type: "lock_profile" });
  });

  elements.settingsSavePin.addEventListener("click", () => {
    const pin = elements.settingsSessionPin.value;
    if (!send({ type: "set_profile_pin", settings: { pin } })) {
      return;
    }
    elements.settingsSessionPin.value = "";
  });

  elements.settingsUnlockProfile.addEventListener("click", () => {
    const pin = elements.settingsSessionPin.value;
    if (!send({ type: "unlock_profile", settings: { pin } })) {
      return;
    }
    elements.settingsSessionPin.value = "";
  });

  elements.settingsCreateBackup.addEventListener("click", () => {
    const password = elements.settingsBackupPassword.value;
    if (!send({ type: "create_backup", settings: { password } })) {
      return;
    }
    elements.settingsBackupPassword.value = "";
  });

  elements.settingsRerunReadiness?.addEventListener("click", () => {
    send({ type: "rerun_readiness" });
  });

  elements.settingsRefreshLicense?.addEventListener("click", () => {
    send({ type: "rerun_license_check" });
  });

  elements.settingsImportLicense?.addEventListener("click", () => {
    elements.settingsLicenseFileInput?.click();
  });

  elements.settingsLicenseFileInput?.addEventListener("change", async (event) => {
    const input = event.target;
    const file = input?.files?.[0];
    if (!file) {
      return;
    }
    const form = new FormData();
    form.append("license_file", file);
    try {
      const response = await secureFetch("/api/license/import", {
        method: "POST",
        body: form,
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail?.detail || t("settings.license_import_failed"));
      }
      send({ type: "rerun_license_check" });
    } catch (error) {
      console.error("[KERN] license import failed:", error);
      window.alert(error?.message || t("settings.license_import_failed"));
    } finally {
      if (elements.settingsLicenseFileInput) {
        elements.settingsLicenseFileInput.value = "";
      }
    }
  });

  elements.settingsExportSupportBundle?.addEventListener("click", async () => {
    try {
      const response = await secureFetch("/support/export", { method: "POST" });
      if (!response.ok) {
        throw new Error(t("settings.support_bundle_failed"));
      }
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") || "";
      const match = disposition.match(/filename=\"?([^\";]+)\"?/i);
      const fileName = match?.[1] || `kern-support-${new Date().toISOString().slice(0, 10)}.zip`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      send({ type: "rerun_readiness" });
    } catch (error) {
      console.error("[KERN] support bundle export failed:", error);
      window.alert(t("settings.support_bundle_failed"));
    }
  });

  elements.syncMailboxButton?.addEventListener("click", () => {
    send({ type: "sync_mailbox", settings: { limit: 8 } });
  });

  elements.utilityToggle.addEventListener("click", () => {
    utilityController.open();
  });

  elements.utilityTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      renderer.activateUtilityTab(tab.dataset.tab);
    });
  });

  elements.settingsSectionNavItems.forEach((item) => {
    item.addEventListener("click", () => {
      renderer.activateSettingsSection(item.dataset.settingsSectionNav);
    });
  });

  elements.settingsThemeButtons?.forEach((button) => {
    button.addEventListener("click", () => {
      themeController.setPreference(button.dataset.themeMode || "system");
      renderer.renderThemeState();
    });
  });

  elements.settingsContent?.addEventListener("scroll", renderer.syncSettingsSectionFromScroll);

  function closeLanguageMenu() {
    elements.settingsLanguageMenu?.classList.add("hidden");
    elements.settingsLanguageButton?.setAttribute("aria-expanded", "false");
  }

  function syncLanguagePicker(lang) {
    const resolvedLang = lang === "de" ? "de" : "en";
    if (elements.settingsLanguage) {
      elements.settingsLanguage.value = resolvedLang;
    }
    if (elements.settingsLanguageLabel) {
      elements.settingsLanguageLabel.textContent = t(
        resolvedLang === "de" ? "settings.language_german" : "settings.language_english"
      );
    }
    elements.settingsLanguageOptions?.forEach((option) => {
      const active = option.dataset.languageOption === resolvedLang;
      option.classList.toggle("is-active", active);
      option.setAttribute("aria-selected", String(active));
    });
  }

  syncLanguagePicker(getCurrentLang());

  elements.settingsLanguageButton?.addEventListener("click", (event) => {
    event.stopPropagation();
    const isOpen = !elements.settingsLanguageMenu?.classList.contains("hidden");
    if (isOpen) {
      closeLanguageMenu();
      return;
    }
    elements.settingsLanguageMenu?.classList.remove("hidden");
    elements.settingsLanguageButton?.setAttribute("aria-expanded", "true");
  });

  elements.settingsLanguageOptions?.forEach((option) => {
    option.addEventListener("click", async () => {
      const lang = option.dataset.languageOption === "de" ? "de" : "en";
      syncLanguagePicker(lang);
      closeLanguageMenu();
      localStorage.setItem("kern.ui.language", lang);
      await loadLocale(lang);
      location.reload();
    });
  });

  elements.settingsLanguageMenu?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeLanguageMenu();
      elements.settingsLanguageButton?.focus();
    }
  });

  elements.knowledgeSearchButton?.addEventListener("click", () => {
    send({ type: "search_knowledge", settings: { query: elements.knowledgeQuery.value.trim() } });
  });

  elements.knowledgeQuery?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      send({ type: "search_knowledge", settings: { query: elements.knowledgeQuery.value.trim() } });
    }
  });

  elements.localModeToggle.addEventListener("change", () => {
    send({ type: "update_settings", settings: { local_mode_enabled: elements.localModeToggle.checked } });
  });

  elements.commandInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      elements.commandForm?.requestSubmit();
    }
  });

  elements.commandInput.addEventListener("input", () => {
    renderer.autoResizeCommandInput();
    renderer.syncConversationState();
  });

  elements.commandInput.addEventListener("focus", () => {
    renderer.syncConversationState();
    requestAnimationFrame(() => {
      elements.threadList.scrollTo({ top: elements.threadList.scrollHeight, behavior: "smooth" });
    });
  });

  elements.commandInput.addEventListener("blur", () => {
    renderer.syncConversationState();
  });

  // Composer plus menu
  function closePlusMenu() {
    elements.composerPlusMenu?.classList.add("hidden");
    elements.composerPlusButton?.setAttribute("aria-expanded", "false");
  }

  elements.composerPlusButton?.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = !elements.composerPlusMenu?.classList.contains("hidden");
    if (isOpen) {
      closePlusMenu();
    } else {
      elements.composerPlusMenu?.classList.remove("hidden");
      elements.composerPlusButton?.setAttribute("aria-expanded", "true");
    }
  });

  document.addEventListener("click", (e) => {
    if (
      !elements.composerPlusMenu?.classList.contains("hidden") &&
      !elements.composerPlusMenu?.contains(e.target) &&
      e.target !== elements.composerPlusButton
    ) {
      closePlusMenu();
    }

    if (
      !elements.settingsLanguageMenu?.classList.contains("hidden") &&
      !elements.settingsLanguageMenu?.contains(e.target) &&
      !elements.settingsLanguageButton?.contains(e.target)
    ) {
      closeLanguageMenu();
    }
  });

  elements.composerAttachFile?.addEventListener("click", () => {
    elements.composerFileInput?.click();
    closePlusMenu();
  });

  async function requestUpload(files) {
    const formData = new FormData();
    files.forEach((file) => formData.append("files", file, file.name));
    try {
      const response = await secureFetch("/upload", { method: "POST", body: formData });
      const data = await response.json().catch((err) => {
        console.error("[KERN] upload response parse:", err);
        return {};
      });
      if (!response.ok) {
        return {
          ok: false,
          error: data.detail || t("composer.upload_failed"),
          data,
        };
      }
      return { ok: true, data };
    } catch (error) {
      console.error("[KERN] upload request failed:", error);
      return { ok: false, error: t("composer.upload_failed") };
    }
  }

  elements.composerFileInput?.addEventListener("change", async () => {
    const files = [...(elements.composerFileInput?.files || [])];
    if (!files.length) return;
    const validationErr = validateUploadFiles(files);
    if (validationErr) {
      setComposerAssist(validationErr, "error");
      if (elements.composerFileInput) elements.composerFileInput.value = "";
      return;
    }
    renderPendingUploads(files);
    setComposerAssist(t("composer.uploading", { count: files.length }), "warning");
    const result = await requestUpload(files);
    if (!result.ok) {
      const payload = result.data || {};
      const rejected = Number(payload.rejected || 0);
      const duplicates = Number(payload.duplicates || 0);
      const failed = Number(payload.failed || 0);
      const presentation = describeUploadFailure(payload, result.error);
      setComposerAssist(presentation.body, "error");
      renderUploadOutcome({
        tone: "error",
        pill: t("upload.pill_error"),
        title: presentation.title,
        meta: t("upload.card_error_meta"),
        body: presentation.body,
        documents: [],
        items: payload.items || [],
        queueMeta: uploadMetaText({ indexed: 0, duplicates, rejected, failed }),
      });
      if (elements.composerFileInput) elements.composerFileInput.value = "";
      return;
    }
    const data = result.data || {};
    const indexedCount = Number(data.indexed ?? 0);
    const duplicatesCount = Number(data.duplicates ?? 0);
    const rejectedCount = Number(data.rejected ?? 0);
    const failedCount = Number(data.failed ?? 0);
    const duplicateOnly = indexedCount === 0 && duplicatesCount > 0 && rejectedCount === 0 && failedCount === 0;
    const mixedBatch = rejectedCount > 0 || failedCount > 0 || duplicatesCount > 0;
    const reviewCount = rejectedCount + failedCount;
    clearComposerAssist();
    renderUploadOutcome({
      tone: duplicateOnly || data.rejected || data.failed ? "warning" : "success",
      pill: t("upload.pill_indexed"),
      title: duplicateOnly
        ? t("upload.duplicate_title", { count: duplicatesCount })
        : mixedBatch && indexedCount > 0
          ? t("upload.mixed_title", { indexed: indexedCount, total: data.total ?? files.length })
        : indexedCount === 1
          ? t("upload.success_title_one")
          : t("upload.success_title_many", { count: indexedCount || files.length }),
      meta: t("upload.card_success_meta", { indexed: data.indexed ?? 0, total: data.total ?? files.length }),
      body: [
        duplicateOnly
          ? t("upload.duplicate_body")
          : mixedBatch
            ? t("upload.mixed_body")
            : indexedCount === 1
              ? t("upload.success_body_indexed")
              : t("upload.bulk_success_body"),
        data.ocr_low_confidence_count ? t("upload.ocr_warning", { count: data.ocr_low_confidence_count }) : "",
      ].filter(Boolean).join("\n"),
      documents: data.documents || [],
      items: data.items || [],
      queueMeta: uploadMetaText({
        indexed: data.indexed ?? 0,
        duplicates: data.duplicates ?? 0,
        rejected: data.rejected ?? 0,
        failed: data.failed ?? 0,
      }),
      stats: {
        indexed: indexedCount,
        duplicates: duplicatesCount,
        review: reviewCount,
      },
    });
    if (elements.composerFileInput) elements.composerFileInput.value = "";
  });

  elements.composerKbAction?.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = !elements.composerKbPicker?.classList.contains("hidden");
    elements.composerKbPicker?.classList.toggle("hidden", isOpen);
    elements.composerKbAction?.setAttribute("aria-expanded", String(!isOpen));
  });

  elements.composerKbSearch?.addEventListener("input", () => {
    const q = elements.composerKbSearch.value.toLowerCase();
    elements.composerKbList?.querySelectorAll(".composer-kb-list__item").forEach((item) => {
      item.style.display = item.textContent.toLowerCase().includes(q) ? "" : "none";
    });
  });

  // Audit export
  document.getElementById("exportAuditButton")?.addEventListener("click", () => {
    send({ type: "export_audit" });
  });

  // Audit category filter
  document.getElementById("auditCategoryFilter")?.addEventListener("change", (e) => {
    const category = e.target.value;
    renderer.setAuditCategoryFilter(category);
  });

  // Schedule management
  const addScheduleButton = document.getElementById("addScheduleButton");
  const addScheduleForm = document.getElementById("addScheduleForm");
  const cancelScheduleButton = document.getElementById("cancelScheduleButton");
  const saveScheduleButton = document.getElementById("saveScheduleButton");
  const scheduleFrequency = document.getElementById("scheduleFrequency");
  const scheduleCron = document.getElementById("scheduleCron");

  addScheduleButton?.addEventListener("click", () => {
    addScheduleForm?.classList.remove("hidden");
    addScheduleButton.classList.add("hidden");
  });

  cancelScheduleButton?.addEventListener("click", () => {
    addScheduleForm?.classList.add("hidden");
    addScheduleButton?.classList.remove("hidden");
  });

  scheduleFrequency?.addEventListener("change", () => {
    if (scheduleCron) {
      if (scheduleFrequency.value === "custom") {
        scheduleCron.classList.remove("hidden");
      } else {
        scheduleCron.classList.add("hidden");
        scheduleCron.value = scheduleFrequency.value;
      }
    }
  });

  saveScheduleButton?.addEventListener("click", () => {
    const title = document.getElementById("scheduleTitle")?.value.trim();
    const cronValue = scheduleFrequency?.value === "custom"
      ? scheduleCron?.value.trim()
      : scheduleFrequency?.value;
    const actionType = document.getElementById("scheduleActionType")?.value;
    const actionText = document.getElementById("scheduleActionPayload")?.value.trim();
    if (!title || !cronValue) return;
    send({
      type: "create_schedule",
      settings: {
        title,
        cron_expression: cronValue,
        action_type: actionType || "custom_prompt",
        action_payload: { prompt: actionText },
      },
    });
    addScheduleForm?.classList.add("hidden");
    addScheduleButton?.classList.remove("hidden");
    if (document.getElementById("scheduleTitle")) document.getElementById("scheduleTitle").value = "";
    if (document.getElementById("scheduleActionPayload")) document.getElementById("scheduleActionPayload").value = "";
  });

  // --- Drag-drop / bulk file upload ---
  let _uploadInProgress = false;

  async function uploadFiles(files, { source = "utility" } = {}) {
    const fileList = Array.from(files || []);
    if (!fileList.length) return;
    if (_uploadInProgress) return;
    const validationErr = validateUploadFiles(fileList);
    const progressEl = document.getElementById("uploadProgress");
    const fillEl = document.getElementById("uploadProgressFill");
    const labelEl = document.getElementById("uploadProgressLabel");
    const uploadBtn = document.getElementById("bulkUploadButton");
    if (validationErr) {
      if (source === "workspace-drop") {
        showWorkspaceDropOverlay("error", { message: validationErr });
        hideWorkspaceDropOverlay({ delayed: true });
      }
      if (progressEl) progressEl.classList.remove("hidden");
      if (labelEl) labelEl.textContent = validationErr;
      renderUploadOutcome({
        tone: "error",
        pill: t("upload.pill_error"),
        title: t("upload.error_title"),
        meta: t("upload.card_error_meta"),
        body: validationErr,
        documents: [],
      });
      setTimeout(() => { if (progressEl) progressEl.classList.add("hidden"); }, 3000);
      return;
    }
    _uploadInProgress = true;
    renderPendingUploads(fileList);
    if (progressEl) progressEl.classList.remove("hidden");
    if (labelEl) labelEl.textContent = t("composer.uploading", { count: fileList.length });
    if (fillEl) fillEl.style.width = "30%";
    if (uploadBtn) uploadBtn.disabled = true;

    if (source === "workspace-drop") {
      showWorkspaceDropOverlay("uploading", { count: fileList.length });
      setWorkspaceDropProgress(true, {
        label: t("dropzone.progress", { count: fileList.length }),
        percent: 34,
      });
    }

    requestUpload(fileList)
      .then((result) => {
        if (!result.ok) {
          const error = new Error(result.error || t("upload.error_body"));
          error.payload = result.data || {};
          throw error;
        }
        const data = result.data || {};
        const indexedCount = Number(data.indexed ?? 0);
        const duplicatesCount = Number(data.duplicates ?? 0);
        const rejectedCount = Number(data.rejected ?? 0);
        const failedCount = Number(data.failed ?? 0);
        const duplicateOnly = indexedCount === 0 && duplicatesCount > 0 && rejectedCount === 0 && failedCount === 0;
        const mixedBatch = rejectedCount > 0 || failedCount > 0 || duplicatesCount > 0;
        const reviewCount = rejectedCount + failedCount;
        if (fillEl) fillEl.style.width = "100%";
        if (labelEl) labelEl.textContent = t("composer.indexed", { indexed: data.indexed ?? 0, total: data.total ?? fileList.length });
        renderUploadOutcome({
          tone: data.rejected || data.failed ? "warning" : "success",
          pill: t("upload.pill_indexed"),
          title: duplicateOnly
            ? t("upload.duplicate_title", { count: duplicatesCount })
            : mixedBatch && indexedCount > 0
              ? t("upload.mixed_title", { indexed: indexedCount, total: data.total ?? fileList.length })
            : indexedCount === 1
              ? t("upload.success_title_one")
              : t("upload.success_title_many", { count: indexedCount || fileList.length }),
          meta: t("upload.card_success_meta", { indexed: data.indexed ?? 0, total: data.total ?? fileList.length }),
          body: [
            duplicateOnly
              ? t("upload.duplicate_body")
              : mixedBatch
                ? t("upload.mixed_body")
                : t("upload.bulk_success_body"),
            data.ocr_low_confidence_count ? t("upload.ocr_warning", { count: data.ocr_low_confidence_count }) : "",
          ].filter(Boolean).join("\n"),
          documents: data.documents || [],
          items: data.items || [],
          queueMeta: uploadMetaText({
            indexed: data.indexed ?? 0,
            duplicates: data.duplicates ?? 0,
            rejected: data.rejected ?? 0,
            failed: data.failed ?? 0,
          }),
          stats: {
            indexed: indexedCount,
            duplicates: duplicatesCount,
            review: reviewCount,
          },
        });
          if (source === "workspace-drop") {
            showWorkspaceDropOverlay("success", { count: fileList.length });
            setWorkspaceDropProgress(true, {
              label: t("composer.indexed", { indexed: data.indexed ?? 0, total: data.total ?? fileList.length }),
              percent: 100,
            });
          }
        setTimeout(() => {
          if (progressEl) progressEl.classList.add("hidden");
          if (fillEl) fillEl.style.width = "0%";
          if (source === "workspace-drop") {
            hideWorkspaceDropOverlay({ delayed: false });
          }
        }, 2500);
      })
        .catch((err) => {
          console.error("[KERN] drag-drop upload failed:", err);
          const errorMessage = err?.message || t("composer.upload_failed");
          const payload = err?.payload || {};
          const presentation = describeUploadFailure(payload, errorMessage);
          if (labelEl) labelEl.textContent = errorMessage;
          renderUploadOutcome({
            tone: "error",
            pill: t("upload.pill_error"),
            title: presentation.title,
            meta: t("upload.card_error_meta"),
            body: presentation.body,
            documents: [],
            items: payload.items || [],
            queueMeta: uploadMetaText({
              indexed: payload.indexed ?? 0,
            duplicates: payload.duplicates ?? 0,
            rejected: payload.rejected ?? 0,
            failed: payload.failed ?? 0,
          }),
          });
            if (source === "workspace-drop") {
              showWorkspaceDropOverlay("error", { message: presentation.body });
            }
        if (fillEl) fillEl.style.width = "0%";
        setTimeout(() => {
          if (progressEl) progressEl.classList.add("hidden");
          if (source === "workspace-drop") {
            hideWorkspaceDropOverlay({ delayed: false });
          }
        }, 2500);
      })
      .finally(() => {
        _uploadInProgress = false;
        if (uploadBtn) uploadBtn.disabled = false;
      });
  }

  const dropZone = document.getElementById("dropZone");
  if (dropZone) {
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropZone.classList.add("drag-over");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropZone.classList.remove("drag-over");
      uploadFiles(e.dataTransfer?.files);
    });
  }

  function resetWorkspaceDragOverlay() {
    workspaceDragDepth = 0;
    if (!_uploadInProgress) {
      hideWorkspaceDropOverlay({ delayed: false });
    }
  }

  elements.workspaceMain?.addEventListener("dragenter", (event) => {
    if (!isFileTransfer(event)) {
      return;
    }
    event.preventDefault();
    workspaceDragDepth = 1;
    showWorkspaceDropOverlay("ready");
  });

  elements.workspaceMain?.addEventListener("dragover", (event) => {
    if (!isFileTransfer(event)) {
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = "copy";
    }
    showWorkspaceDropOverlay("ready");
  });

  elements.workspaceMain?.addEventListener("dragleave", (event) => {
    if (!isFileTransfer(event)) {
      return;
    }
    event.preventDefault();
    if (event.relatedTarget instanceof Node && elements.workspaceMain?.contains(event.relatedTarget)) {
      return;
    }
    workspaceDragDepth = 0;
    if (!_uploadInProgress) {
      hideWorkspaceDropOverlay({ delayed: false });
    }
  });

  elements.workspaceMain?.addEventListener("drop", (event) => {
    if (!isFileTransfer(event)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    workspaceDragDepth = 0;
    const files = Array.from(event.dataTransfer?.files || []);
    if (!files.length) {
      hideWorkspaceDropOverlay({ delayed: false });
      return;
    }
    uploadFiles(files, { source: "workspace-drop" });
  });

  window.addEventListener("dragend", resetWorkspaceDragOverlay);
  window.addEventListener("drop", (event) => {
    if (elements.workspaceMain?.contains(event.target)) {
      return;
    }
    resetWorkspaceDragOverlay();
  });

  const bulkFileInput = document.getElementById("bulkFileInput");
  bulkFileInput?.addEventListener("change", () => {
    uploadFiles(bulkFileInput.files);
    bulkFileInput.value = "";
  });

  const dismissAllAlertsButton = document.getElementById("dismissAllAlertsButton");
  dismissAllAlertsButton?.addEventListener("click", () => {
    send({ type: "dismiss_all_alerts" });
  });

  const kgSearchButton = document.getElementById("kgSearchButton");
  const kgSearchInput = document.getElementById("kgSearchInput");
  const kgBuildButton = document.getElementById("kgBuildButton");

  kgSearchButton?.addEventListener("click", () => {
    const q = kgSearchInput?.value.trim();
    if (q) send({ type: "search_knowledge_graph", settings: { query: q } });
  });
  kgSearchInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") kgSearchButton?.click();
  });
  kgBuildButton?.addEventListener("click", () => {
    send({ type: "submit_text", text: t("kg.build_prompt") });
  });

  // Load graph when Intelligence tab is activated
  document.querySelectorAll(".utility-tab[data-tab='intelligence']").forEach((btn) => {
    btn.addEventListener("click", () => {
      send({ type: "get_knowledge_graph", settings: {} });
    });
  });

  const memorySearchButton = document.getElementById("memorySearchButton");
  const memorySearchInput = document.getElementById("memorySearchInput");
  const memoryDateFrom = document.getElementById("memoryDateFrom");
  const memoryDateTo = document.getElementById("memoryDateTo");

  function doMemorySearch() {
    const query = memorySearchInput?.value.trim();
    if (!query) return;
    send({
      type: "search_memory_history",
      settings: {
        query,
        date_from: memoryDateFrom?.value || null,
        date_to: memoryDateTo?.value || null,
      },
    });
  }

  memorySearchButton?.addEventListener("click", doMemorySearch);
  memorySearchInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doMemorySearch();
  });
}
