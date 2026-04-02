import { t } from "/static/js/i18n.js";
import { escapeHTML } from "/static/js/utils.js";

const SIDEBAR_COLLAPSED_KEY = "kern.sidebar.collapsed";

export function createDashboardRenderer({ elements, send, themeController, passageController }) {
  let pendingSnapshot = null;
  let snapshotFrameScheduled = false;
  let lastConversationKey = "";
  let currentSnapshot = null;
  let onboardingUiState = null;
  let conversationPrimed = false;
  let auditCategoryFilter = "";
  let llmStreamElement = null;
  let llmStreamBody = null;
  let dismissedFailureKey = "";
  const lastRenderedKeys = {
    conversation: "",
    context: "",
    capabilities: "",
    receipts: "",
    runtime: "",
  };

  function locale() {
    return document.documentElement.lang || undefined;
  }

  function formatDate(date, options) {
    return new Intl.DateTimeFormat(locale(), options).format(date);
  }

  function formatTimestamp(timestamp) {
    if (!timestamp) return "";
    return formatDate(new Date(timestamp), {
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatRelativeDate(timestamp) {
    if (!timestamp) return t("chat.no_date");
    const date = new Date(timestamp);
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const startOfTarget = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const diffDays = Math.round((startOfToday - startOfTarget) / 86400000);
    if (diffDays === 0) {
      return t("chat.today");
    }
    if (diffDays === 1) {
      return t("chat.yesterday");
    }
    if (diffDays > 1 && diffDays < 7) {
      return formatDate(date, { weekday: "long" });
    }
    return formatDate(date, { month: "short", day: "numeric" });
  }

  function truncate(text, limit = 40) {
    if (!text) return "";
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  }

  function formatSettingValue(value, fallback = null) {
    if (fallback === null) fallback = t("settings.not_configured");
    return value ? value : fallback;
  }

  function titleCase(value) {
    return String(value || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function summarizeFailureMessage(failure) {
    const code = String(failure?.error_code || "").toLowerCase();
    const blockedScope = String(failure?.blocked_scope || "").toLowerCase();
    const source = String(failure?.source || "").toLowerCase();
    const title = String(failure?.title || "");

    if (code === "document_ingest_failed" || blockedScope.includes("document") || source === "upload" || /^ingest\b/i.test(title)) {
      return t("failures.document_ingest_summary");
    }
    if (code === "upload_invalid" || source === "upload") {
      return t("failures.upload_summary");
    }
    if (code === "model_path_missing" || code === "model_path_invalid" || code === "local_runtime_unreachable") {
      return t("failures.model_runtime_summary");
    }
    if (code === "support_bundle_failed") {
      return t("failures.support_bundle_summary");
    }
    if (code === "backup_failed" || code === "restore_failed" || code === "update_failed") {
      return t("failures.recovery_summary");
    }
    return failure?.message || t("failures.default_message");
  }

  function summarizeFailureTitle(failure) {
    const code = String(failure?.error_code || "").toLowerCase();
    const blockedScope = String(failure?.blocked_scope || "").toLowerCase();
    const source = String(failure?.source || "").toLowerCase();
    const title = String(failure?.title || "");

    if (code === "document_ingest_failed" || blockedScope.includes("document") || source === "upload" || /^ingest\b/i.test(title)) {
      return t("failures.document_ingest_title");
    }
    if (code === "upload_invalid") {
      return t("failures.upload_title");
    }
    if (code === "model_path_missing" || code === "model_path_invalid" || code === "local_runtime_unreachable") {
      return t("failures.model_runtime_title");
    }
    if (code === "support_bundle_failed") {
      return t("failures.support_bundle_title");
    }
    if (code === "backup_failed" || code === "restore_failed" || code === "update_failed") {
      return t("failures.recovery_title");
    }
    return failure?.title || t("failures.default_title");
  }

  function isDocumentFailure(failure) {
    const code = String(failure?.error_code || "").toLowerCase();
    const blockedScope = String(failure?.blocked_scope || "").toLowerCase();
    const source = String(failure?.source || "").toLowerCase();
    const title = String(failure?.title || "");
    return code === "document_ingest_failed" || code === "upload_invalid" || blockedScope.includes("document") || source === "upload" || /^ingest\b/i.test(title);
  }

  function cleanTechnicalDetail(detail) {
    const raw = String(detail || "").trim();
    if (!raw) {
      return "";
    }
    return raw
      .replace(/\s+/g, " ")
      .replace(/\s*\(at\s+\.\.\\paddle\\fluid\\framework\\new_executor\\instruction\\onednn\\onednn_instruction\.cc:118\)\s*/gi, "")
      .trim();
  }

  function buildFailureKey(failures = []) {
    return failures
      .map((failure) => `${failure?.id || ""}:${failure?.error_code || ""}:${failure?.title || ""}:${failure?.message || ""}`)
      .join("|");
  }

  function getOnboardingRenderState(snapshot) {
    const serverOnboarding = snapshot?.onboarding || {};
    if (onboardingUiState) {
      const stepMatched = onboardingUiState.untilStep && serverOnboarding.current_step === onboardingUiState.untilStep;
      const inactiveMatched = onboardingUiState.untilInactive && !serverOnboarding.active;
      if (stepMatched || inactiveMatched) {
        onboardingUiState = null;
      }
    }
    const state = onboardingUiState;
    return {
      onboarding: state?.override ? { ...serverOnboarding, ...state.override } : serverOnboarding,
      pending: Boolean(state?.pending),
    };
  }

  function updateConnectionState(state) {
    if (!elements.connectionState) {
      return;
    }
    const label =
      state === "connected"
        ? t("status.connected")
        : state === "reconnecting"
          ? t("status.reconnecting")
          : state === "offline"
            ? t("status.offline")
            : t("status.connecting");
    elements.connectionState.textContent = label;
    elements.connectionState.dataset.state = state;
  }

  function updateClock() {
    const now = new Date();
    elements.topTimestamp.textContent = formatDate(now, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function applySidebarCollapsed(collapsed) {
    elements.workspaceShell.classList.toggle("sidebar-collapsed", collapsed);
    elements.sidebarToggle.setAttribute("aria-label", collapsed ? t("nav.expand_sidebar") : t("nav.collapse_sidebar"));
    elements.sidebarToggle.title = collapsed ? t("nav.expand_sidebar") : t("nav.collapse_sidebar");
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
  }

  function autoResizeCommandInput() {
    elements.commandInput.style.height = "0px";
    elements.commandInput.style.height = `${Math.min(elements.commandInput.scrollHeight, 136)}px`;
  }

  function syncConversationState(turns = currentSnapshot?.conversation_turns || []) {
    const hasTurns = Boolean(turns.length);
    const hasDraft = Boolean(elements.commandInput.value.trim());
    const hasFocus = document.activeElement === elements.commandInput;
    const isBusy = Boolean(currentSnapshot?.action_in_progress);
    const engaged = conversationPrimed || hasTurns || hasDraft || hasFocus || isBusy;
    elements.conversationShell.classList.toggle("is-engaged", engaged);
  }

  function scrollThreadToLatest(behavior = "smooth") {
    elements.threadList.scrollTo({ top: elements.threadList.scrollHeight, behavior });
  }

  function isThreadNearBottom() {
    return elements.threadList.scrollHeight - elements.threadList.scrollTop - elements.threadList.clientHeight < 84;
  }

  function renderKey(snapshot, key) {
    return snapshot?.render_keys?.[key] || "";
  }

  function renderList(listElement, items, formatter, emptyText = null) {
    if (emptyText === null) emptyText = t("misc.nothing_to_report");
    if (!listElement) return;
    listElement.innerHTML = "";
    if (!items || items.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = emptyText;
      listElement.appendChild(empty);
      return;
    }

    items.forEach((item) => {
      const li = document.createElement("li");
      li.textContent = formatter(item);
      listElement.appendChild(li);
    });
  }

  function createStateListItem(title, body, tone = "empty", pill = "") {
    const li = document.createElement("li");
    li.className = "detail-list__item detail-list__item--state";

    const card = document.createElement("div");
    card.className = `panel-state panel-state--${tone}`;

    if (title || pill) {
      const head = document.createElement("div");
      head.className = "panel-state__head";

      if (title) {
        const heading = document.createElement("strong");
        heading.className = "panel-state__title";
        heading.textContent = title;
        head.appendChild(heading);
      }

      if (pill) {
        const badge = document.createElement("span");
        badge.className = "panel-state__pill";
        badge.textContent = pill;
        head.appendChild(badge);
      }

      card.appendChild(head);
    }

    if (body) {
      const text = document.createElement("p");
      text.className = "panel-state__body";
      text.textContent = body;
      card.appendChild(text);
    }

    li.appendChild(card);
    return li;
  }

  function applyProductPosture(snapshot) {
    const posture = snapshot?.product_posture === "personal" ? "personal" : "production";
    document.body.dataset.productPosture = posture;
    if (elements.workspaceShell) {
      elements.workspaceShell.dataset.productPosture = posture;
    }
    if (elements.commandInput) {
      elements.commandInput.placeholder = t("composer.placeholder");
    }
  }

  function isSampleDocument(doc) {
    if (!doc) return false;
    const category = String(doc.category || "").toLowerCase();
    const tags = Array.isArray(doc.tags) ? doc.tags.map((tag) => String(tag || "").toLowerCase()) : [];
    return category === "sample_workspace" || tags.includes("sample_workspace") || tags.includes("demo");
  }

  function buildDocumentLabel(doc) {
    const parts = [];
    const baseType = doc.category || doc.file_type || t("docs.default_type");
    if (baseType) {
      parts.push(baseType);
    }
    if (isSampleDocument(doc)) {
      parts.push(t("docs.sample_badge"));
    }
    if (doc.archived) {
      parts.push(t("docs.archived_badge"));
    }
    return parts.join(" / ");
  }

  function renderOnboarding(snapshot) {
      if (!elements.onboardingCard || !elements.onboardingModal) {
        return;
      }
      const renderState = getOnboardingRenderState(snapshot);
      const onboarding = renderState.onboarding || {};
      const trust = snapshot.trust_summary || {};
    const stepLabels = {
      storage: t("onboarding.step_storage"),
      model: t("onboarding.step_model"),
      workflow: t("onboarding.step_workflow"),
      sample: t("onboarding.step_sample"),
      done: t("onboarding.step_done"),
    };
      const isActive = Boolean(onboarding.active);
      elements.onboardingModal.classList.toggle("hidden", !isActive);
      elements.onboardingModal.setAttribute("aria-hidden", isActive ? "false" : "true");
      elements.onboardingCard.classList.toggle("hidden", !isActive);
      elements.onboardingCard.classList.toggle("is-pending", Boolean(renderState.pending));
      elements.onboardingCard.setAttribute("aria-busy", renderState.pending ? "true" : "false");
      if (elements.onboardingPrimaryAction) {
        elements.onboardingPrimaryAction.disabled = Boolean(renderState.pending);
      }
      if (elements.onboardingSecondaryAction) {
        elements.onboardingSecondaryAction.disabled = Boolean(renderState.pending);
      }
      if (elements.onboardingDismiss) {
        elements.onboardingDismiss.disabled = Boolean(renderState.pending);
      }
      if (!isActive) {
        return;
      }
    elements.onboardingCard.dataset.step = onboarding.current_step || "storage";
    elements.onboardingEyebrow.textContent = t("onboarding.eyebrow");
    elements.onboardingStepLabel.textContent = stepLabels[onboarding.current_step] || t("onboarding.step_storage");
    elements.onboardingTitle.textContent = onboarding.title || t("onboarding.default_title");
    elements.onboardingBody.textContent = onboarding.body || t("onboarding.default_body");
    elements.onboardingLocalPosture.textContent = trust.local_posture || t("onboarding.local_default");
    elements.onboardingStoragePath.textContent = onboarding.storage_path || trust.storage_posture || t("settings.not_configured");
    elements.onboardingModelPath.textContent = onboarding.model_path
      ? `${onboarding.model_note || t("settings.ai_default")} / ${onboarding.model_path}`
      : trust.model_posture || t("settings.not_configured");
    elements.onboardingRecoveryPath.textContent = trust.recovery_posture || t("onboarding.recovery_default");
    elements.onboardingReadiness.textContent = trust.readiness_posture || t("onboarding.readiness_default");
    if (elements.onboardingActivationNote) {
      elements.onboardingActivationNote.textContent = onboarding.activation_note || t("onboarding.activation_default");
    }
    elements.onboardingPrimaryAction.textContent = onboarding.primary_action || t("onboarding.primary_default");
    elements.onboardingSecondaryAction.textContent = onboarding.secondary_action || t("onboarding.secondary_default");
    elements.onboardingDismiss.textContent = t("onboarding.skip");
    elements.onboardingSecondaryAction.classList.toggle("hidden", !onboarding.secondary_action);
    elements.onboardingDismiss.classList.toggle("hidden", !["workflow", "sample"].includes(onboarding.current_step));
    if (elements.emptyLead) {
      elements.emptyLead.textContent = onboarding.current_step === "sample"
        ? t("empty.lead_sample")
        : onboarding.current_step === "workflow"
          ? t("empty.lead_workflow")
          : t("empty.lead");
    }
  }

  function renderFailures(snapshot) {
      if (!elements.failurePanel || !elements.failureList) {
        return;
      }
      const failures = snapshot.active_failures || [];
      const failureKey = buildFailureKey(failures);
      if (dismissedFailureKey && dismissedFailureKey !== failureKey) {
        dismissedFailureKey = "";
      }
      const isDismissed = Boolean(failureKey) && dismissedFailureKey === failureKey;
      elements.failurePanel.classList.toggle("hidden", failures.length === 0);
      elements.failureList.innerHTML = "";
      if (!failures.length || isDismissed) {
        elements.failurePanel.classList.toggle("hidden", true);
        return;
      }

    const li = document.createElement("li");
    const card = document.createElement("article");
    card.className = "failure-card";

    const head = document.createElement("div");
    head.className = "failure-card__head";
    const title = document.createElement("strong");
    title.className = "failure-card__title";
    const allDocumentFailures = failures.every(isDocumentFailure);
    title.textContent = allDocumentFailures ? t("failures.document_ingest_title") : summarizeFailureTitle(failures[0]);
      const pill = document.createElement("span");
      pill.className = "failure-card__pill";
      pill.textContent = t("failures.items_count", { count: failures.length });
      const meta = document.createElement("div");
      meta.className = "failure-card__head-meta";
      meta.appendChild(pill);
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "failure-card__dismiss";
      dismiss.setAttribute("aria-label", t("failures.dismiss"));
      dismiss.textContent = "\u00d7";
      dismiss.addEventListener("click", () => {
        dismissedFailureKey = failureKey;
        renderFailures(currentSnapshot || { active_failures: [] });
      });
      meta.appendChild(dismiss);
      head.appendChild(title);
      head.appendChild(meta);

    const body = document.createElement("p");
    body.className = "failure-card__meta";
    body.textContent = allDocumentFailures && failures.length > 1
      ? t("failures.document_ingest_summary_many", { count: failures.length })
      : summarizeFailureMessage(failures[0]);

    card.appendChild(head);
    card.appendChild(body);

    const details = document.createElement("details");
    details.className = "failure-card__details";
    const summary = document.createElement("summary");
    summary.className = "failure-card__details-summary";
    summary.textContent = t("failures.see_more");
    details.appendChild(summary);

    const detailBody = document.createElement("div");
    detailBody.className = "failure-card__details-body";

    const nextAction = failures.find((failure) => failure.next_action)?.next_action;
    if (nextAction) {
      const next = document.createElement("p");
      next.className = "failure-card__next";
      next.textContent = nextAction;
      detailBody.appendChild(next);
    }

    const retryable = failures.find((failure) => failure.retry_available && failure.retry_action);
    if (retryable) {
      const actions = document.createElement("div");
      actions.className = "failure-card__actions";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "ghost-button";
      retry.textContent = t("failures.retry");
      retry.addEventListener("click", () => {
        send({ type: "retry_failure_action", settings: { failure_id: retryable.id, retry_action: retryable.retry_action } });
      });
      actions.appendChild(retry);
      detailBody.appendChild(actions);
    }

    if (failures.length > 1) {
      const list = document.createElement("ul");
      list.className = "failure-card__detail-list";
      failures.forEach((failure) => {
        const item = document.createElement("li");
        item.className = "failure-card__detail-item";
        item.textContent = failure.title || summarizeFailureTitle(failure);
        list.appendChild(item);
      });
      detailBody.appendChild(list);
    }

    const technicalParts = failures
      .map((failure) => {
        const cleaned = cleanTechnicalDetail(failure.technical_detail);
        if (!cleaned) {
          return "";
        }
        return `${failure.title || summarizeFailureTitle(failure)}\n${cleaned}`;
      })
      .filter(Boolean);
    if (technicalParts.length) {
      const detail = document.createElement("pre");
      detail.className = "failure-card__technical";
      detail.textContent = technicalParts.join("\n\n");
      detailBody.appendChild(detail);
    }

    details.appendChild(detailBody);
    card.appendChild(details);
    li.appendChild(card);
    elements.failureList.appendChild(li);
  }

  function renderReminderList(items) {
    elements.remindersList.innerHTML = "";
    if (!items || items.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = t("misc.no_reminders");
      elements.remindersList.appendChild(empty);
      return;
    }

    items.forEach((item) => {
      const li = document.createElement("li");
      const body = document.createElement("div");
      const date = new Date(item.due_at);
      body.textContent = `${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} ${item.title}`;
      li.appendChild(body);

      if (item.id) {
        const actions = document.createElement("div");
        actions.className = "reminder-actions";

        const snooze = document.createElement("button");
        snooze.type = "button";
        snooze.className = "inline-action";
        snooze.textContent = t("actions.snooze");
        snooze.addEventListener("click", () => {
          send({ type: "reminder_action", settings: { action: "snooze", reminder_id: item.id, minutes: 10 } });
        });

        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "inline-action";
        dismiss.textContent = t("actions.dismiss");
        dismiss.addEventListener("click", () => {
          send({ type: "reminder_action", settings: { action: "dismiss", reminder_id: item.id } });
        });

        actions.appendChild(snooze);
        actions.appendChild(dismiss);
        li.appendChild(actions);
      }

      elements.remindersList.appendChild(li);
    });
  }

  function syncInputDeviceDropdown() {
    const dropdown = document.getElementById("inputDeviceDropdown");
    const label = document.getElementById("inputDeviceLabel");
    const list = document.getElementById("inputDeviceList");
    if (!dropdown || !label || !list) return;

    const select = elements.inputDevice;
    const currentValue = select.value;

    list.innerHTML = "";
    [...select.options].forEach((opt) => {
      const li = document.createElement("li");
      li.className = "custom-select__option" + (opt.value === currentValue ? " is-selected" : "");
      li.textContent = opt.textContent;
      li.dataset.value = opt.value;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", opt.value === currentValue ? "true" : "false");
      li.addEventListener("click", () => {
        select.value = opt.value;
        select.dispatchEvent(new Event("change"));
        syncInputDeviceDropdown();
        dropdown.classList.remove("is-open");
        document.getElementById("inputDeviceTrigger").setAttribute("aria-expanded", "false");
      });
      list.appendChild(li);
    });

    const selectedOption = select.options[select.selectedIndex];
    label.textContent = selectedOption ? selectedOption.textContent : t("ops.default_device");
  }

  function renderInputDevices(devices, selected) {
    if (!elements.inputDevice) return;
    const previous = elements.inputDevice.value;
    elements.inputDevice.innerHTML = "";

    const autoOption = document.createElement("option");
    autoOption.value = "";
    autoOption.textContent = t("ops.default_device");
    elements.inputDevice.appendChild(autoOption);

    (devices || []).forEach((device) => {
      const option = document.createElement("option");
      option.value = String(device.index);
      option.textContent = `${device.name} (${device.channels}ch)`;
      elements.inputDevice.appendChild(option);
    });

    if (selected !== null && selected !== undefined) {
      elements.inputDevice.value = String(selected);
    } else if (previous && [...elements.inputDevice.options].some((option) => option.value === previous)) {
      elements.inputDevice.value = previous;
    } else {
      elements.inputDevice.value = "";
    }

    syncInputDeviceDropdown();
  }

  function createTurnElement(turn) {
    const li = document.createElement("li");
    li.className = `thread-turn thread-turn--${turn.role}`;
    if (turn.id) {
      li.dataset.turnId = turn.id;
    }

    const meta = document.createElement("div");
    meta.className = "thread-turn__meta";
    meta.textContent = `${turn.role === "user" ? t("chat.you") : turn.role === "assistant" ? t("chat.kern") : t("chat.system")}${
      turn.timestamp ? ` · ${formatTimestamp(turn.timestamp)}` : ""
    }`;

    const bubble = document.createElement("div");
    bubble.className = "thread-turn__bubble";

    if (turn.kind !== "message" || turn.status !== "complete") {
      const badge = document.createElement("div");
      badge.className = "thread-turn__badge";
      badge.dataset.kind = turn.kind;
      badge.dataset.status = turn.status;
      badge.textContent = `${turn.kind.replace(/_/g, " ")} · ${turn.status}`;
      bubble.appendChild(badge);
    }

    const body = document.createElement("div");
    body.className = "thread-turn__body";
    body.textContent = turn.text;
    bubble.appendChild(body);

    li.appendChild(meta);
    li.appendChild(bubble);
    return li;
  }

  function renderThread(turns, shouldAutoFollow = true) {
    elements.threadList.innerHTML = "";
    const items = turns || [];
    elements.emptyState.classList.toggle("hidden", items.length > 0);
    syncConversationState(items);

    items.forEach((turn) => {
      elements.threadList.appendChild(createTurnElement(turn));
    });

    const conversationKey = items.map((turn) => turn.id).join("|");
    if (conversationKey !== lastConversationKey && shouldAutoFollow) {
      requestAnimationFrame(() => {
        scrollThreadToLatest(lastConversationKey ? "smooth" : "auto");
      });
      lastConversationKey = conversationKey;
    } else if (conversationKey !== lastConversationKey) {
      lastConversationKey = conversationKey;
    }
  }

  function renderSessionList(turns, filterText = "") {
    elements.sessionList.innerHTML = "";
    const items = turns || [];
    const normalizedFilter = filterText.trim().toLowerCase();

    if (items.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = t("session.no_items");
      elements.sessionList.appendChild(empty);
      return;
    }

    const createSessionButton = (title, meta, onClick) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sidebar-session-item";

      const strong = document.createElement("strong");
      strong.textContent = title;

      const small = document.createElement("small");
      small.textContent = meta;

      button.appendChild(strong);
      button.appendChild(small);
      button.addEventListener("click", onClick);
      return button;
    };

    if (normalizedFilter) {
      const matches = items
        .filter((turn) => turn.text.toLowerCase().includes(normalizedFilter))
        .slice()
        .reverse()
        .slice(0, 8);

      if (matches.length === 0) {
        const empty = document.createElement("li");
        empty.textContent = t("session.no_matches");
        elements.sessionList.appendChild(empty);
        return;
      }

      matches.forEach((turn) => {
        const li = document.createElement("li");
        const roleLabel = turn.role === "user" ? t("chat.you") : turn.role === "assistant" ? t("chat.kern") : t("chat.system");
        li.appendChild(
          createSessionButton(
            truncate(turn.text, 52),
            `${roleLabel}${turn.timestamp ? ` · ${formatTimestamp(turn.timestamp)}` : ""}`,
            () => {
              elements.commandInput.value = turn.role === "user" ? turn.text : elements.commandInput.value;
              autoResizeCommandInput();
              elements.commandInput.focus();
            }
          )
        );
        elements.sessionList.appendChild(li);
      });
      return;
    }

    const firstUserTurn = items.find((turn) => turn.role === "user");
    const latestTurn = items.at(-1);
    const sessionTitle = firstUserTurn ? truncate(firstUserTurn.text, 52) : t("session.current");
    const sessionMeta = `${items.length} turns${latestTurn?.timestamp ? ` · ${formatTimestamp(latestTurn.timestamp)}` : ""}`;

    const li = document.createElement("li");
    li.appendChild(
      createSessionButton(sessionTitle, sessionMeta, () => {
        scrollThreadToLatest("smooth");
        syncConversationState(items);
        autoResizeCommandInput();
        requestAnimationFrame(() => {
          elements.commandInput.focus();
        });
      })
    );
    elements.sessionList.appendChild(li);
  }

  function getConversationSearchItems(filterText = "") {
    const turns = currentSnapshot?.conversation_turns || [];
    const normalizedFilter = filterText.trim().toLowerCase();
    const items = turns
      .filter((turn) => !normalizedFilter || turn.text.toLowerCase().includes(normalizedFilter))
      .slice()
      .reverse()
      .slice(0, 24);

    return items.map((turn) => ({
      id: turn.id,
      title: truncate(turn.text || t("search.untitled_message"), 72),
      role: turn.role === "user" ? t("chat.you") : turn.role === "assistant" ? t("chat.kern") : t("chat.system"),
      timestamp: turn.timestamp,
      relativeDate: formatRelativeDate(turn.timestamp),
      exactTime: turn.timestamp ? formatDate(new Date(turn.timestamp), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : t("search.no_timestamp"),
    }));
  }

  function renderConversationSearch(filterText = "") {
    if (!elements.conversationSearchResults) {
      return;
    }
    elements.conversationSearchResults.innerHTML = "";
    const items = getConversationSearchItems(filterText);
    if (!items.length) {
      const empty = document.createElement("li");
      empty.className = "search-results-list__empty";
      empty.textContent = filterText.trim() ? t("search.no_match") : t("search.no_messages");
      elements.conversationSearchResults.appendChild(empty);
      return;
    }

    items.forEach((item, index) => {
      const li = document.createElement("li");
      li.dataset.testid = "conversation-search-result-row";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "search-result";
      button.dataset.turnId = item.id || "";
      button.dataset.testid = "conversation-search-result";
      button.setAttribute("aria-label", `${item.title} ${item.relativeDate}`);

      const copy = document.createElement("div");
      copy.className = "search-result__copy";

      const eyebrow = document.createElement("span");
      eyebrow.className = "search-result__eyebrow";
      eyebrow.textContent = item.role;

      const title = document.createElement("strong");
      title.textContent = item.title;

      const meta = document.createElement("small");
      meta.textContent = item.exactTime;

      copy.appendChild(eyebrow);
      copy.appendChild(title);
      copy.appendChild(meta);

      const date = document.createElement("span");
      date.className = "search-result__date";
      date.textContent = index === 0 && !filterText.trim() ? t("chat.latest") : item.relativeDate;

      button.appendChild(copy);
      button.appendChild(date);
      li.appendChild(button);
      elements.conversationSearchResults.appendChild(li);
    });
  }

  function scrollToTurn(turnId) {
    if (!turnId) {
      return;
    }
    const target = elements.threadList.querySelector(`[data-turn-id="${turnId}"]`);
    if (!target) {
      return;
    }
    target.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function renderPlan(plan) {
    renderList(
      elements.planList,
      plan?.steps || [],
      (step, index) => `${index + 1}. ${step.capability_name}${Object.keys(step.arguments || {}).length ? ` (${Object.keys(step.arguments).join(", ")})` : ""}`,
      t("plan.none")
    );
  }

  function renderContext(context) {
    if (context?.summary_lines?.length) {
      renderList(elements.contextList, context.summary_lines, (line) => line, t("context.none"));
      return;
    }
    const facts = context?.facts || [];
    renderList(elements.contextList, facts, (fact) => `${fact.key}: ${fact.value}`, t("context.none"));
  }

  function renderReceipts(receipts) {
    renderList(
      elements.receiptList,
      receipts || [],
      (receipt) => `[${receipt.status}] ${receipt.capability_name} - ${receipt.message}`,
      t("receipts.none")
    );
  }

  function renderKnowledge(snapshot) {
    const retrieval = snapshot.retrieval_status || {};
    elements.knowledgeBackend.textContent = (retrieval.backend || "lexical").replace(/_/g, " ");
    elements.knowledgeState.textContent = retrieval.reason || t("knowledge.ready");
    if (elements.knowledgeQuery && elements.knowledgeQuery.value.trim() === "" && snapshot.last_retrieval_query) {
      elements.knowledgeQuery.value = snapshot.last_retrieval_query;
    }
    const hits = snapshot.recent_retrieval_hits || [];
    elements.knowledgeResults.innerHTML = "";
    if (!hits.length) {
      elements.knowledgeResults.appendChild(
        createStateListItem(
          snapshot.last_retrieval_query ? t("knowledge.no_results") : t("knowledge.empty_title"),
          snapshot.last_retrieval_query ? t("knowledge.no_results_detail") : t("knowledge.prompt_search_detail"),
          snapshot.last_retrieval_query ? "warning" : "empty",
          snapshot.last_retrieval_query ? t("knowledge.search") : t("knowledge.title")
        )
      );
      return;
    }
    hits.forEach((hit) => {
      const li = document.createElement("li");
      li.textContent = `${hit.source_type} / ${hit.metadata?.title || hit.source_id} / score ${Number(hit.score || 0).toFixed(2)} / ${truncate(hit.text || "", 88)}`;
      elements.knowledgeResults.appendChild(li);
    });
  }

  function renderCapabilities(capabilities) {
    elements.capabilityList.innerHTML = "";
    const items = capabilities || [];
    if (!items.length) {
      elements.capabilityList.appendChild(
        createStateListItem(t("capabilities.empty_title"), t("capabilities.empty_detail"), "empty", t("settings.ai_models"))
      );
      return;
    }

    items.forEach((capability) => {
      const li = document.createElement("li");
      const availability = capability.available ? t("capabilities.available") : t("capabilities.unavailable");
      const status = capability.last_status ? ` / ${capability.last_status}` : "";
      li.textContent = `${capability.title} - ${availability} / ${capability.verification_support}${status}`;
      if (capability.notes) {
        const note = document.createElement("small");
        note.textContent = capability.notes;
        li.appendChild(note);
      }
      elements.capabilityList.appendChild(li);
    });
  }

  function renderOverview(snapshot) {
    if (
      !elements.overviewProfileState ||
      !elements.overviewProfileMeta ||
      !elements.overviewKnowledgeState ||
      !elements.overviewKnowledgeMeta ||
      !elements.overviewSecurityState ||
      !elements.overviewSecurityMeta ||
      !elements.overviewSyncState ||
      !elements.overviewSyncMeta
    ) {
      return;
    }
    const profile = snapshot.active_profile || {};
    const session = snapshot.profile_session || {};
    const retrieval = snapshot.retrieval_status || {};
    const security = snapshot.security_status || {};
    const syncTargets = snapshot.sync_targets || [];
    const locked = session.unlocked === false;
    const uploadOnlyCount = syncTargets.filter((target) => target.status === "upload_only").length;
    const degradedTargets = syncTargets.filter((target) => target.status === "degraded").length;
    const readyTargets = syncTargets.filter((target) => (target.status || "ready") === "ready").length;

    elements.overviewProfileState.textContent = locked ? t("ops.locked") : t("ops.unlocked");
    elements.overviewProfileMeta.textContent = `${profile.slug || "default"} / ${(snapshot.memory_scope || "profile").replace(/_/g, " ")}`;

    elements.overviewKnowledgeState.textContent = titleCase(retrieval.index_health || retrieval.backend || "disabled");
    elements.overviewKnowledgeMeta.textContent = retrieval.reason || t("overview.backend", { backend: titleCase(retrieval.backend || "lexical") });

    elements.overviewSecurityState.textContent = snapshot.audit_chain_ok ? t("audit.verified") : t("sync.degraded");
    elements.overviewSecurityMeta.textContent = snapshot.audit_chain_ok
      ? `${security.db_encryption_enabled ? t("overview.db_encrypted", { mode: titleCase(security.db_encryption_mode || t("overview.encrypted")) }) : t("overview.db_unencrypted")} / ${security.artifact_encryption_enabled ? t("overview.artifacts_encrypted") : t("overview.artifacts_limited")}`
      : snapshot.audit_chain_reason || t("overview.audit_attention");

    if (!syncTargets.length) {
      elements.overviewSyncState.textContent = t("sync.local_only");
      elements.overviewSyncMeta.textContent = t("sync.local_only_meta");
      return;
    }

    if (degradedTargets) {
      elements.overviewSyncState.textContent = t("sync.degraded");
      elements.overviewSyncMeta.textContent = t("sync.degraded_meta", { count: degradedTargets });
      return;
    }

    if (uploadOnlyCount) {
      elements.overviewSyncState.textContent = t("sync.upload_only");
      elements.overviewSyncMeta.textContent = t("sync.upload_only_meta", { upload_count: uploadOnlyCount, ready_count: readyTargets });
      return;
    }

    elements.overviewSyncState.textContent = t("sync.ready");
    elements.overviewSyncMeta.textContent = t("sync.ready_meta", { count: readyTargets });
  }

  function renderThemeState() {
    if (!themeController) {
      return;
    }
    const { preference, activeTheme } = themeController.getState();
    const preferenceLabel = titleCase(preference);
    const activeLabel = titleCase(activeTheme);
    const note =
      preference === "system"
        ? t("settings.theme_note_system", { active: activeLabel })
        : t("settings.theme_note_locked", { active: activeLabel });

    if (elements.settingsThemePreference) {
      elements.settingsThemePreference.textContent = preferenceLabel;
    }
    if (elements.settingsActiveTheme) {
      elements.settingsActiveTheme.textContent = activeLabel;
    }
    if (elements.settingsAppearancePreference) {
      elements.settingsAppearancePreference.textContent = preferenceLabel;
    }
    if (elements.settingsAppearanceActiveTheme) {
      elements.settingsAppearanceActiveTheme.textContent = activeLabel;
    }
    if (elements.settingsThemeNote) {
      elements.settingsThemeNote.textContent = note;
    }
    elements.settingsThemeButtons?.forEach((button) => {
      const isActive = button.dataset.themeMode === preference;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-checked", isActive ? "true" : "false");
    });
  }

  function renderModelInfo(snapshot) {
    const info = snapshot.model_info || {};
    if (elements.settingsVersion) {
      elements.settingsVersion.textContent = `v${info.app_version || "0.0.0"}`;
    }
    if (elements.settingsModelName) {
      elements.settingsModelName.textContent = info.cognition_name || t("settings.ai_default");
    }
    if (elements.settingsModelType) {
      elements.settingsModelType.textContent = info.cognition_type || t("settings.hybrid_stack");
    }
    if (elements.settingsModelBackend) {
      elements.settingsModelBackend.textContent = info.cognition_backend || snapshot.cognition_backend || "hybrid";
    }
    if (elements.settingsModelMode) {
      elements.settingsModelMode.textContent = info.model_mode || "off";
    }
    if (elements.settingsModelPath) {
      elements.settingsModelPath.textContent = formatSettingValue(info.cognition_model_path);
    }
    if (elements.settingsFastModelPath) {
      elements.settingsFastModelPath.textContent = formatSettingValue(info.fast_model_path);
    }
    if (elements.settingsDeepModelPath) {
      elements.settingsDeepModelPath.textContent = formatSettingValue(info.deep_model_path);
    }
    if (elements.settingsEmbedModel) {
      elements.settingsEmbedModel.textContent = formatSettingValue(info.embed_model);
    }
    if (elements.settingsRetrievalBackend) {
      elements.settingsRetrievalBackend.textContent = snapshot.retrieval_status?.backend || "lexical";
    }
    if (elements.settingsRetrievalHealth) {
      elements.settingsRetrievalHealth.textContent = snapshot.retrieval_status?.index_health || "disabled";
    }
    if (elements.settingsVoiceBackend) {
      elements.settingsVoiceBackend.textContent = info.voice_model
        ? `${info.voice_backend || "none"} / ${info.voice_model}`
        : info.voice_backend || "none";
    }
    if (elements.settingsCloudMode) {
      elements.settingsCloudMode.textContent = info.cloud_available ? t("settings.cloud_available") : t("settings.local_only");
    }
    if (elements.settingsModelStory) {
      const llmReady = snapshot.llm_available !== false;
      const cloudAllowed = Boolean(info.cloud_available);
      const isolated = snapshot.network_status?.status === "isolated";
      const tone = llmReady ? "success" : "warning";
      const facts = [
        t("settings.model_fact_runtime", { runtime: info.preferred_runtime || t("settings.ai_default") }),
        t("settings.model_fact_model", { model: info.cognition_name || t("settings.ai_default") }),
        cloudAllowed ? t("settings.model_fact_cloud_enabled") : t("settings.model_fact_cloud_disabled"),
        isolated ? t("settings.model_fact_isolated") : t("settings.model_fact_local_endpoint"),
        t("settings.model_fact_fail_closed"),
      ];
      elements.settingsModelStory.dataset.tone = tone;
      elements.settingsModelStoryPill.textContent = llmReady ? t("settings.model_pill_ready") : t("settings.model_pill_attention");
      elements.settingsModelStoryTitle.textContent = llmReady ? t("settings.model_story_ready_title") : t("settings.model_story_warning_title");
      elements.settingsModelStoryText.textContent = llmReady
        ? info.preferred_runtime_detail || t("settings.model_story_ready_body")
        : t("settings.model_story_warning_body");
      elements.settingsModelStoryFacts.innerHTML = "";
      facts.forEach((fact) => {
        const li = document.createElement("li");
        li.textContent = fact;
        elements.settingsModelStoryFacts.appendChild(li);
      });
    }
    if (elements.settingsHybridDetails) {
      elements.settingsHybridDetails.innerHTML = "";
      (info.hybrid_details || []).forEach((detail) => {
        const li = document.createElement("li");
        li.textContent = detail;
        elements.settingsHybridDetails.appendChild(li);
      });
    }
  }

  let _selectedDocIds = new Set();

  function renderComposerKbPicker(docs) {
    const list = elements.composerKbList;
    if (!list) return;
    list.innerHTML = "";
    if (!docs || docs.length === 0) {
      const empty = document.createElement("li");
      empty.className = "composer-kb-list__empty";
      empty.textContent = t("docs.no_documents");
      list.appendChild(empty);
      return;
    }
    docs.forEach((doc) => {
      const li = document.createElement("li");
      li.className = "composer-kb-list__item";
      const title = document.createElement("span");
      title.className = "composer-kb-list__title";
      title.textContent = doc.title || doc.source_id || t("docs.untitled");
      const meta = document.createElement("span");
      meta.className = "composer-kb-list__meta";
      meta.textContent = buildDocumentLabel(doc);
      li.appendChild(title);
      li.appendChild(meta);
      li.addEventListener("click", () => {
        const ref = `@${doc.title || doc.source_id}`;
        if (elements.commandInput) {
          elements.commandInput.value = elements.commandInput.value
            ? `${elements.commandInput.value} ${ref}`
            : ref;
          elements.commandInput.dispatchEvent(new Event("input"));
          elements.commandInput.focus();
        }
        document.getElementById("composerPlusMenu")?.classList.add("hidden");
        document.getElementById("composerPlusButton")?.setAttribute("aria-expanded", "false");
      });
      list.appendChild(li);
    });
  }

  function renderDocumentsBrowser(docs) {
    const list = document.getElementById("documentsList");
    if (!list) return;
    list.innerHTML = "";
    if (!docs || docs.length === 0) {
      list.appendChild(
        createStateListItem(t("docs.empty_title"), t("docs.empty_detail"), "empty", t("docs.upload"))
      );
      _updateCompareButton();
      return;
    }
    docs.forEach((doc) => {
      const li = document.createElement("li");
      li.className = "detail-list__item";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "doc-select-checkbox";
      checkbox.dataset.docId = doc.id;
      checkbox.checked = _selectedDocIds.has(doc.id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          _selectedDocIds.add(doc.id);
        } else {
          _selectedDocIds.delete(doc.id);
        }
        _updateCompareButton();
      });
      const copy = document.createElement("div");
      copy.className = "detail-list__copy";
      const label = document.createElement("span");
      label.className = "detail-list__label";
      label.textContent = doc.title || t("docs.untitled");
      const meta = document.createElement("span");
      meta.className = "detail-list__meta";
      meta.textContent = buildDocumentLabel(doc);
      li.appendChild(checkbox);
      copy.appendChild(label);
      copy.appendChild(meta);
      li.appendChild(copy);
      list.appendChild(li);
    });
    _updateCompareButton();
  }

  function _updateCompareButton() {
    let btn = document.getElementById("compareDocsButton");
    const list = document.getElementById("documentsList");
    if (!list) return;
    if (_selectedDocIds.size >= 2) {
      if (!btn) {
        btn = document.createElement("button");
        btn.id = "compareDocsButton";
        btn.type = "button";
        btn.className = "ghost-button";
        btn.style.marginTop = "6px";
        btn.addEventListener("click", () => {
          const ids = [..._selectedDocIds];
          const query = window.prompt(
            t("docs.compare_prompt"),
            t("docs.compare_default_query")
          );
          if (query === null) {
            return;
          }
          send({ type: "submit_text", text: `compare_documents ${JSON.stringify(ids)} :: ${query}` });
          _selectedDocIds.clear();
          renderDocumentsBrowser(currentSnapshot?.recent_documents || []);
        });
        list.parentElement?.appendChild(btn);
      }
      btn.textContent = t("docs.compare", { count: _selectedDocIds.size });
      btn.classList.remove("hidden");
    } else if (btn) {
      btn.classList.add("hidden");
    }
  }

  function renderTrustBadge(networkStatus) {
    const badge = document.getElementById("trustBadge");
    const label = document.getElementById("trustBadgeLabel");
    if (!badge || !label) return;
    const status = networkStatus.status || "checking";
    badge.className = "trust-badge";
    if (status === "isolated") {
      badge.classList.add("trust-badge--isolated");
      label.textContent = t("trust.fully_local");
      badge.title = t("trust.fully_local_detail");
    } else if (status === "unmonitored") {
      badge.classList.add("trust-badge--checking");
      label.textContent = t("trust.unmonitored");
      badge.title = t("trust.unmonitored_detail");
    } else if (status === "network_detected") {
      badge.classList.add("trust-badge--network");
      const count = networkStatus.outbound_connections || 0;
      label.textContent = t("trust.network_detected", { count });
      badge.title = (networkStatus.endpoints || []).slice(0, 5).join(", ") || t("trust.network_detected_detail");
    } else {
      badge.classList.add("trust-badge--checking");
      label.textContent = t("trust.checking");
    }
  }

  function renderAuditLog(events, networkStatus) {
    const list = document.getElementById("auditLogList");
    const networkEl = document.getElementById("auditNetworkStatus");
    const networkDetail = document.getElementById("auditNetworkDetail");
    if (networkEl) {
      const status = networkStatus.status || "checking";
      networkEl.textContent = status === "isolated"
        ? t("trust.fully_local")
        : status === "unmonitored"
          ? t("trust.unmonitored")
          : status === "network_detected"
            ? t("audit.outbound_count", { count: networkStatus.outbound_connections || 0 })
            : t("trust.checking_detail");
      networkEl.className = `meta-text audit-status--${status === "isolated" ? "success" : status === "network_detected" ? "failure" : ""}`;
    }
    if (networkDetail && networkStatus.last_check) {
      networkDetail.textContent = networkStatus.status === "network_detected"
        ? t("audit.unexpected_outbound", { endpoints: (networkStatus.endpoints || []).slice(0, 3).join(", ") })
        : networkStatus.status === "unmonitored"
          ? t("trust.unmonitored_detail")
        : t("audit.last_checked", { time: formatDate(new Date(networkStatus.last_check), { hour: "2-digit", minute: "2-digit", second: "2-digit" }) });
    }
    if (!list) return;
    list.innerHTML = "";
    const filteredEvents = auditCategoryFilter
      ? events.filter((event) => String(event.category || "").toLowerCase() === auditCategoryFilter)
      : events;
    if (!filteredEvents.length) {
      const li = document.createElement("li");
      li.className = "detail-list__item detail-list__item--empty";
      li.textContent = auditCategoryFilter ? t("audit.no_events_filter") : t("audit.no_events");
      list.appendChild(li);
      return;
    }
    filteredEvents.forEach((event) => {
      const li = document.createElement("li");
      li.className = "detail-list__item";
      const cat = document.createElement("span");
      cat.className = "audit-category";
      cat.textContent = event.category;
      const msg = document.createElement("span");
      msg.textContent = `${event.action}: ${event.message}`;
      const status = document.createElement("span");
      status.className = `audit-status audit-status--${event.status}`;
      status.textContent = event.status;
      li.appendChild(cat);
      li.appendChild(msg);
      li.appendChild(status);
      list.appendChild(li);
    });
  }

  function renderSchedules(tasks) {
    const list = document.getElementById("scheduleList");
    if (!list) return;
    list.innerHTML = "";
    if (!tasks || !tasks.length) {
      list.appendChild(
        createStateListItem(t("schedules.empty_title"), t("schedules.empty_detail"), "empty", t("schedules.add"))
      );
      return;
    }
    tasks.forEach((task) => {
      const li = document.createElement("li");
      li.className = "detail-list__item";
      li.dataset.testid = "schedule-row";
      const enabled = task.enabled !== false;
      const status = task.run_status || "idle";
      const failureMeta = task.last_error ? ` / ${truncate(task.last_error, 42)}` : "";
      li.innerHTML = `
        <span class="detail-list__label${enabled ? "" : " detail-list__label--muted"}">${escapeHTML(task.title)}</span>
        <span class="meta-text">${escapeHTML(task.cron_expression || "")} / ${escapeHTML(status)}${task.failure_count ? ` / ${escapeHTML(t("schedules.failures", { count: Number(task.failure_count) }))}` : ""}${escapeHTML(failureMeta)}</span>
        <div class="detail-list__actions">
          <button type="button" class="ghost-button schedule-toggle-btn" data-id="${escapeHTML(task.id)}" data-enabled="${enabled}" title="${enabled ? t("schedules.disable") : t("schedules.enable")}">${enabled ? t("schedules.disable") : t("schedules.enable")}</button>
          ${status === "failed" ? `<button type="button" class="ghost-button schedule-retry-btn" data-id="${escapeHTML(task.id)}" title="${t("schedules.retry")}">${t("schedules.retry")}</button>` : ""}
          <button type="button" class="ghost-button schedule-delete-btn" data-id="${escapeHTML(task.id)}" title="${t("schedules.delete")}">${t("schedules.delete")}</button>
        </div>`;
      list.appendChild(li);
    });
    list.querySelectorAll(".schedule-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.id;
        const currentlyEnabled = btn.dataset.enabled === "true";
        send({ type: "toggle_schedule", settings: { schedule_id: id, enabled: !currentlyEnabled } });
      });
    });
    list.querySelectorAll(".schedule-delete-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        send({ type: "delete_schedule", settings: { schedule_id: btn.dataset.id } });
      });
    });
    list.querySelectorAll(".schedule-retry-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        send({ type: "retry_failed_task", settings: { schedule_id: btn.dataset.id } });
      });
    });
  }

  function renderProactiveAlerts(alerts) {
    const block = document.getElementById("proactiveAlertsBlock");
    const list = document.getElementById("proactiveAlertsList");
    const badge = document.getElementById("alertBadge");
    if (!list) return;
    if (!alerts || !alerts.length) {
      if (block) block.style.display = "none";
      if (badge) badge.classList.add("hidden");
      return;
    }
    if (block) block.style.display = "";
    if (badge) badge.classList.remove("hidden");
    list.innerHTML = "";
    alerts.forEach((alert, idx) => {
      const li = document.createElement("li");
      li.className = "detail-list__item";
      li.dataset.testid = "proactive-alert-row";
      const title = document.createElement("span");
      title.className = "proactive-alert__title";
      title.textContent = alert.title || t("schedules.alert_default");
      const meta = document.createElement("span");
      meta.className = "supporting-copy";
      const metaParts = [];
      if (alert.priority) metaParts.push(String(alert.priority).toUpperCase());
      if (alert.interruption_class) metaParts.push(String(alert.interruption_class).replaceAll("_", " "));
      if (typeof alert.confidence === "number") metaParts.push(t("alerts.confidence", { count: Math.round(alert.confidence * 100) }));
      if (alert.reason) metaParts.push(alert.reason);
      meta.textContent = metaParts.join(" · ");
      const message = document.createElement("span");
      message.className = "proactive-alert__message";
      message.textContent = alert.message || "";
      const actions = document.createElement("div");
      actions.className = "proactive-alert__actions";
      const suggestedActions = Array.isArray(alert.suggested_actions) ? alert.suggested_actions : [];
      if (suggestedActions.length) {
        suggestedActions.forEach((sa) => {
            const saBtn = document.createElement("button");
            saBtn.type = "button";
            saBtn.className = "ghost-button";
            saBtn.dataset.testid = "proactive-alert-action";
            saBtn.textContent = sa.label || sa.action_type;
            saBtn.addEventListener("click", () => {
              send({ type: "execute_suggested_action", settings: { action_type: sa.action_type, action_payload: sa.payload || {}, alert_index: idx } });
            });
            actions.appendChild(saBtn);
          });
        } else {
        const actionBtn = document.createElement("button");
        actionBtn.type = "button";
        actionBtn.className = "ghost-button";
        actionBtn.dataset.testid = "proactive-alert-action";
        actionBtn.textContent = t("schedules.take_action");
        actionBtn.addEventListener("click", () => {
          send({ type: "submit_text", text: alert.message });
        });
        actions.appendChild(actionBtn);
      }
      const dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "ghost-button";
      dismissBtn.dataset.testid = "proactive-alert-dismiss";
      dismissBtn.textContent = t("schedules.dismiss");
      dismissBtn.addEventListener("click", () => {
        send({ type: "dismiss_alert", settings: { alert_index: idx } });
      });
      actions.appendChild(dismissBtn);
      li.appendChild(title);
      if (meta.textContent) li.appendChild(meta);
      li.appendChild(message);
      li.appendChild(actions);
      list.appendChild(li);
    });
  }

  let _kgInstance = null;

  function renderKnowledgeGraph(graphData) {
    const canvas = document.getElementById("kgCanvas");
    const block = document.getElementById("kgCanvasBlock");
    const status = document.getElementById("kgStatus");
    if (!canvas) return;
    const nodes = graphData.nodes || [];
    const links = graphData.links || [];
    if (!nodes.length) {
      if (block) block.style.display = "none";
      if (status) status.textContent = t("empty.no_knowledge_graph");
      return;
    }
    if (block) block.style.display = "";
    if (status) status.textContent = t("kg.status", { nodes: nodes.length, links: links.length });
    import("/static/js/knowledge-graph.js").then(({ KnowledgeGraph }) => {
      if (!_kgInstance) {
        _kgInstance = new KnowledgeGraph(canvas);
      }
      _kgInstance.load(graphData);
    }).catch((err) => {
      console.error("[KERN] knowledge graph load failed:", err);
      if (status) status.textContent = t("kg.unavailable");
    });
  }

  function renderKnowledgeGraphSearch(entities) {
    const block = document.getElementById("kgResultsBlock");
    const list = document.getElementById("kgResultsList");
    if (!list) return;
    if (!entities || !entities.length) {
      if (block) block.style.display = "none";
      return;
    }
    if (block) block.style.display = "";
    list.innerHTML = "";
    entities.forEach((e) => {
      const li = document.createElement("li");
      li.className = "detail-list__item";
      li.dataset.testid = "kg-result-row";
      li.textContent = `[${e.type}] ${e.name}`;
      list.appendChild(li);
    });
  }

  function renderMemoryTimeline(timeline) {
    const block = document.getElementById("memoryTimelineBlock");
    const container = document.getElementById("memoryTimeline");
    const countEl = document.getElementById("memoryTimelineCount");
    if (!container) return;
    if (!timeline || !timeline.length) {
      if (block) block.style.display = "none";
      return;
    }
    if (block) block.style.display = "";
    if (countEl) countEl.textContent = t("memory.dates_count", { count: timeline.length });
    container.innerHTML = "";
    timeline.forEach((group) => {
      const groupEl = document.createElement("div");
      groupEl.className = "memory-timeline__group";
      const dot = document.createElement("span");
      dot.className = "memory-timeline__dot";
      const dateEl = document.createElement("div");
      dateEl.className = "memory-timeline__date";
      dateEl.textContent = group.date || t("misc.unknown");
      const entriesEl = document.createElement("div");
      entriesEl.className = "memory-timeline__entries";
      (group.entries || []).slice(0, 4).forEach((entry) => {
        const entryEl = document.createElement("div");
        entryEl.className = "memory-timeline__entry";
        const content = String(entry.content || "").slice(0, 160);
        entryEl.textContent = content + (entry.content && entry.content.length > 160 ? "…" : "");
        entriesEl.appendChild(entryEl);
      });
      groupEl.appendChild(dot);
      groupEl.appendChild(dateEl);
      groupEl.appendChild(entriesEl);
      container.appendChild(groupEl);
    });
  }

  function renderMemorySearchResults(hits) {
    const block = document.getElementById("memoryResultsBlock");
    const list = document.getElementById("memoryResultsList");
    if (!list) return;
    if (!hits || !hits.length) {
      if (block) block.style.display = "none";
      return;
    }
    if (block) block.style.display = "";
    list.innerHTML = "";
    hits.forEach((hit) => {
      const li = document.createElement("li");
      li.className = "detail-list__item";
      li.dataset.testid = "memory-result-row";
      const date = hit.date || hit.created_at?.slice(0, 10) || "";
      const content = String(hit.content || "").slice(0, 160);
      li.textContent = `[${date}] ${content}${hit.content && hit.content.length > 160 ? "…" : ""}`;
      list.appendChild(li);
    });
  }

  function renderPlatformInfo(snapshot) {
    const profile = snapshot.active_profile || {};
    const session = snapshot.profile_session || {};
    const locked = session.unlocked === false;
    const stateLabel = locked ? `${t("ops.locked")}${session.locked_reason ? ` / ${session.locked_reason}` : ""}` : t("ops.unlocked");
    elements.settingsProfileName.textContent = profile.title ? `${profile.title} (${profile.slug})` : profile.slug || "default";
    elements.settingsProfileState.textContent = stateLabel;
    elements.settingsMemoryScope.textContent = (snapshot.memory_scope || "profile").replace(/_/g, " ");
    elements.settingsDocumentsRoot.textContent = formatSettingValue(snapshot.storage_roots?.documents);
    elements.settingsArchiveRoot.textContent = formatSettingValue(snapshot.storage_roots?.archives);
    elements.settingsBackupRoot.textContent = formatSettingValue(snapshot.storage_roots?.backups);
    elements.settingsReadinessStatus.textContent = snapshot.readiness_summary?.headline || t("settings.readiness_checking");
    elements.settingsAuditState.textContent = snapshot.audit_enabled
      ? snapshot.recent_audit_events?.length
        ? t("audit.enabled_live")
        : t("audit.enabled")
      : t("audit.disabled");
    elements.settingsAuditChain.textContent = snapshot.audit_chain_ok
      ? t("audit.verified")
      : `${t("sync.degraded")}${snapshot.audit_chain_reason ? ` / ${truncate(snapshot.audit_chain_reason, 52)}` : ""}`;
    elements.settingsDbEncryption.textContent = snapshot.security_status?.db_encryption_enabled
      ? `${snapshot.security_status.db_encryption_mode} / ${t("misc.enabled").toLowerCase()}`
      : t("misc.off");
    elements.settingsKeyVersion.textContent = String(snapshot.security_status?.key_version || 0);
    elements.settingsArtifactEncryption.textContent = snapshot.security_status?.artifact_encryption_enabled
      ? `${snapshot.security_status.artifact_encryption_status || t("overview.encrypted")}${snapshot.security_status?.artifact_encryption_migration_state ? ` / ${snapshot.security_status.artifact_encryption_migration_state}` : ""}`
      : t("misc.disabled");
    elements.settingsSupportBundlePath.textContent = snapshot.support_bundle_path || t("settings.support_bundle_none");
    elements.settingsSupportBundleLastExport.textContent = snapshot.support_bundle_last_export_at
      ? formatDate(new Date(snapshot.support_bundle_last_export_at), { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
      : t("settings.not_exported");
    elements.settingsUpdateChannel.textContent = snapshot.update_channel || "stable";
    if (elements.settingsLicenseCard) {
      const license = snapshot.license_summary || {};
      const tone = license.status === "active" || license.status === "trial" ? "success" : license.status === "expired" || license.status === "invalid" ? "warning" : "empty";
      const licensePillMap = {
        unlicensed: t("settings.license_status_unlicensed"),
        trial: t("settings.license_status_trial"),
        active: t("settings.license_status_active"),
        expired: t("settings.license_status_expired"),
        invalid: t("settings.license_status_invalid"),
      };
      const facts = [
        `${t("settings.license_plan")} ${license.plan || t("settings.not_configured")}`,
        `${t("settings.license_mode")} ${license.activation_mode || "offline_license_file"}`,
        `${t("settings.license_install")} ${license.install_id || t("settings.not_configured")}`,
      ];
      if (license.expires_at) {
        facts.push(`${t("settings.license_expires")} ${formatDate(new Date(license.expires_at), { year: "numeric", month: "short", day: "numeric" })}`);
      }
      if (license.grace_state) {
        facts.push(license.grace_state);
      }
      elements.settingsLicenseCard.dataset.tone = tone;
      elements.settingsLicensePill.textContent = licensePillMap[license.status || "unlicensed"] || titleCase(license.status || "unlicensed");
      const commerciallyActive = license.status === "active" || license.status === "trial";
      elements.settingsLicenseTitle.textContent = commerciallyActive
        ? t("settings.license_active_title")
        : t("settings.license_optional_title");
      elements.settingsLicenseBody.textContent = license.message || t("settings.license_default_body");
      elements.settingsLicenseFacts.innerHTML = "";
      facts.forEach((fact) => {
        const li = document.createElement("li");
        li.textContent = fact;
        elements.settingsLicenseFacts.appendChild(li);
      });
    }
    if (elements.composerAttachFile) {
      elements.composerAttachFile.disabled = false;
      elements.composerAttachFile.setAttribute("aria-disabled", "false");
      elements.composerAttachFile.title = t("composer.upload_file_desc");
    }
    if (elements.settingsUpdateCard) {
      const updateState = snapshot.update_state || {};
      const tone = updateState.last_status === "failed" ? "warning" : updateState.last_status === "rollback_performed" ? "warning" : "success";
      const updatePillMap = {
        idle: t("settings.update_status_idle"),
        succeeded: t("settings.update_status_succeeded"),
        failed: t("settings.update_status_failed"),
        rollback_performed: t("settings.update_status_rollback"),
      };
      const facts = [
        `${t("settings.update_channel_label")} ${updateState.channel || snapshot.update_channel || "stable"}`,
        `${t("settings.update_last_backup")} ${updateState.last_backup_at ? formatDate(new Date(updateState.last_backup_at), { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : t("settings.not_exported")}`,
        `${t("settings.update_last_restore")} ${updateState.last_restore_attempt_at ? formatDate(new Date(updateState.last_restore_attempt_at), { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : t("settings.never")}`,
      ];
      if (updateState.last_error) {
        facts.push(`${t("settings.update_last_error")} ${truncate(updateState.last_error, 80)}`);
      }
      elements.settingsUpdateCard.dataset.tone = tone;
      elements.settingsUpdatePill.textContent = updatePillMap[updateState.last_status || "idle"] || titleCase(updateState.last_status || "idle");
      elements.settingsUpdateTitle.textContent = updateState.policy || t("settings.update_default_title");
      elements.settingsUpdateBody.textContent = updateState.message || t("settings.update_default_body");
      elements.settingsUpdateFacts.innerHTML = "";
      facts.forEach((fact) => {
        const li = document.createElement("li");
        li.textContent = fact;
        elements.settingsUpdateFacts.appendChild(li);
      });
    }
    renderOverview(snapshot);
    const totals = snapshot.domain_totals || {};
    elements.settingsDocumentCount.textContent = String(totals.documents || 0);
    elements.settingsEmailAccounts.textContent = String(totals.email_accounts || 0);
    elements.settingsDraftCount.textContent = String(totals.email_drafts || 0);
    elements.settingsMeetingCount.textContent = String(totals.meetings || 0);
    elements.settingsBusinessCount.textContent = String(totals.business_documents || 0);
    elements.settingsSyncCount.textContent = String(totals.sync_targets || 0);
    elements.systemProfileName.textContent = profile.slug || "default";
    elements.systemProfileState.textContent = stateLabel;
    elements.systemMemoryScope.textContent = (snapshot.memory_scope || "profile").replace(/_/g, " ");
    elements.settingsCreateBackup.disabled = locked;
    elements.settingsLockProfile.disabled = locked;
    elements.settingsUnlockProfile.disabled = !locked;
    elements.utilityToggle.disabled = locked;
    if (elements.syncMailboxButton) {
      elements.syncMailboxButton.disabled = locked;
    }
    if (elements.exportLogsLink) {
      elements.exportLogsLink.setAttribute("aria-disabled", locked ? "true" : "false");
      elements.exportLogsLink.tabIndex = locked ? -1 : 0;
      elements.exportLogsLink.style.pointerEvents = locked ? "none" : "auto";
      elements.exportLogsLink.style.opacity = locked ? "0.48" : "1";
      elements.exportLogsLink.title = locked ? t("misc.export_logs_disabled") : t("misc.export_logs_enabled");
    }
    if (elements.settingsExportSupportBundle) {
      elements.settingsExportSupportBundle.disabled = locked;
    }
    if (elements.settingsRerunReadiness) {
      elements.settingsRerunReadiness.disabled = false;
    }

    renderList(
      elements.jobsList,
      snapshot.background_jobs || [],
      (job) => `${job.title} / ${job.status}${job.detail ? ` / ${job.detail}` : ""}`,
      t("jobs.none")
    );
    renderList(
      elements.auditList,
      snapshot.recent_audit_events || [],
      (event) => `[${event.category}] ${event.message}`,
      t("audit.no_events")
    );
    renderList(
      elements.backupTargetsList,
      snapshot.backup_targets || [],
      (target) => `${target.label} / ${target.kind} / ${target.path}`,
      t("backup.no_targets")
    );
    renderList(
      elements.domainNotesList,
      Object.entries(snapshot.domain_statuses || {}).map(([domain, status]) => ({ domain, status })),
      (item) => {
        const state = item.status.ready ? (item.status.degraded ? t("sync.degraded").toLowerCase() : t("sync.ready").toLowerCase()) : t("misc.blocked");
        return `${item.domain} / ${state} / ${item.status.reason}`;
      },
      t("settings.domain_notes_empty")
    );
    renderDocumentsBrowser(snapshot.recent_documents || []);
    renderComposerKbPicker(snapshot.recent_documents || []);
    renderList(
      elements.mailboxList,
      snapshot.recent_email_messages || [],
      (message) => `${truncate(message.sender, 28)} / ${truncate(message.subject, 48)}${message.folder ? ` / ${message.folder}` : ""}`,
      t("mailbox.none")
    );
    renderList(
      elements.emailAccountsList,
      snapshot.email_accounts || [],
      (account) => {
        const health = account.health || account.sync_status || "drafts_only";
        const label = health === "drafts_only" ? t("email.drafts_only") : health.replace(/_/g, " ");
        return `${account.label} / ${label}${account.last_sync_at ? ` / ${formatTimestamp(account.last_sync_at)}` : ""}${account.last_failure ? ` / ${truncate(account.last_failure, 44)}` : ""}`;
      },
      t("email.no_accounts")
    );
    renderList(
      elements.draftsList,
      snapshot.email_drafts || [],
      (draft) => `${draft.status} / ${truncate(draft.subject || t("email.no_subject"), 48)}`,
      t("email.no_drafts")
    );
    elements.emailSuggestionsList.innerHTML = "";
    if (!snapshot.email_reminder_suggestions?.length) {
      const emptySuggestions = document.createElement("li");
      emptySuggestions.textContent = t("email.no_suggestions");
      elements.emailSuggestionsList.appendChild(emptySuggestions);
    } else {
      snapshot.email_reminder_suggestions.forEach((suggestion) => {
        const li = document.createElement("li");
        const text = document.createElement("div");
        text.textContent = `${suggestion.status} / ${truncate(suggestion.title, 48)} / ${formatTimestamp(suggestion.due_at)}`;
        li.appendChild(text);
        if (suggestion.status === "suggested") {
          const actions = document.createElement("div");
          actions.className = "reminder-actions";
          const accept = document.createElement("button");
          accept.type = "button";
          accept.className = "inline-action";
          accept.textContent = t("actions.accept");
          accept.addEventListener("click", () => {
            send({ type: "apply_email_reminder_suggestion", settings: { message_id: suggestion.message_id, accepted: true } });
          });
          const reject = document.createElement("button");
          reject.type = "button";
          reject.className = "inline-action";
          reject.textContent = t("actions.reject");
          reject.addEventListener("click", () => {
            send({ type: "apply_email_reminder_suggestion", settings: { message_id: suggestion.message_id, accepted: false } });
          });
          actions.appendChild(accept);
          actions.appendChild(reject);
          li.appendChild(actions);
        }
        elements.emailSuggestionsList.appendChild(li);
      });
    }
    renderList(
      elements.meetingsList,
      snapshot.recent_meetings || [],
      (meeting) => `${meeting.title} / ${meeting.status || t("meetings.recorded")}`,
      t("empty.no_meetings")
    );
    elements.meetingReviewsList.innerHTML = "";
    if (!snapshot.recent_meeting_reviews?.length) {
      const emptyReviews = document.createElement("li");
      emptyReviews.textContent = t("meetings.no_reviews");
      elements.meetingReviewsList.appendChild(emptyReviews);
    } else {
      snapshot.recent_meeting_reviews.forEach((review) => {
        const li = document.createElement("li");
        const meeting = review.meeting || {};
        const head = document.createElement("div");
        head.textContent = `${meeting.title || t("meetings.default_title")} / ${meeting.status || t("meetings.recorded")}`;
        li.appendChild(head);
        if (review.summary?.content) {
          const summary = document.createElement("div");
          summary.textContent = `${t("meetings.summary_label")}: ${truncate(review.summary.content, 140)}`;
          li.appendChild(summary);
        }
        if (review.transcript?.content) {
          const transcript = document.createElement("div");
          transcript.textContent = `${t("meetings.transcript_label")}: ${truncate(review.transcript.content, 140)}`;
          li.appendChild(transcript);
        }
        (review.action_items || []).slice(0, 3).forEach((item) => {
          const row = document.createElement("div");
          row.textContent = `${item.review_state || t("review.pending")} / ${truncate(item.title || "", 56)}${item.related_task_id ? ` / ${t("review.task")} #${item.related_task_id}` : ""}${item.related_reminder_id ? ` / ${t("review.reminder")} #${item.related_reminder_id}` : ""}`;
          li.appendChild(row);
          if ((item.review_state || "pending") === "pending") {
            const actions = document.createElement("div");
            actions.className = "reminder-actions";
            const accept = document.createElement("button");
            accept.type = "button";
            accept.className = "inline-action";
            accept.textContent = t("actions.accept");
            accept.addEventListener("click", () => {
              send({ type: "review_action_item", settings: { item_id: item.id, accepted: true } });
            });
            const reject = document.createElement("button");
            reject.type = "button";
            reject.className = "inline-action";
            reject.textContent = t("actions.reject");
            reject.addEventListener("click", () => {
              send({ type: "review_action_item", settings: { item_id: item.id, accepted: false } });
            });
            actions.appendChild(accept);
            actions.appendChild(reject);
            li.appendChild(actions);
          }
        });
        elements.meetingReviewsList.appendChild(li);
      });
    }
    renderList(
      elements.businessDocsList,
      snapshot.business_documents || [],
      (doc) => `${doc.title} / ${doc.kind} / ${doc.status}`,
      t("business.none")
    );
    renderList(
      elements.syncTargetsList,
      snapshot.sync_targets || [],
      (target) => `${target.label} / ${target.status === "upload_only" ? t("sync.remote_export") : target.kind} / ${target.status || t("sync.ready").toLowerCase()}${target.last_sync_at ? ` / ${formatTimestamp(target.last_sync_at)}` : ""}${target.last_failure ? ` / ${truncate(target.last_failure, 36)}` : ""}`,
      t("sync.none")
    );
    renderList(
      elements.recoveryList,
      snapshot.recovery_checkpoints || [],
      (checkpoint) => `${checkpoint.job_id} / ${checkpoint.stage}`,
      t("recovery.none")
    );
    renderList(
      elements.backupFilesList,
      snapshot.available_backups || [],
      (path) => truncate(path, 68),
      t("backup.no_files")
    );
  }

  function setActionLock(locked) {
    elements.sendButton.disabled = locked;
    elements.confirmButton.disabled = locked;
    elements.cancelButton.disabled = locked;
    if (elements.syncMailboxButton) {
      elements.syncMailboxButton.disabled = locked;
    }
    if (elements.knowledgeSearchButton) {
      elements.knowledgeSearchButton.disabled = locked;
    }
    if (elements.knowledgeQuery) {
      elements.knowledgeQuery.disabled = locked;
    }
    elements.promptButtons.forEach((button) => {
      button.disabled = locked;
    });
    const disabledReason = locked ? t("actions.unlock_required") : "";
    elements.sendButton.title = disabledReason;
    if (elements.knowledgeSearchButton) {
      elements.knowledgeSearchButton.title = disabledReason;
    }
    if (elements.syncMailboxButton) {
      elements.syncMailboxButton.title = disabledReason;
    }
  }

  function renderRuntimeSlice(snapshot) {
    const currentState = snapshot.assistant_state || "idle";
    const showOnboardingStatus = !snapshot.action_in_progress
      && snapshot.onboarding?.active
      && (!snapshot.last_action || snapshot.last_action === "Waiting for you." || snapshot.last_action === t("status.waiting"));
    if (elements.statusText) {
      elements.statusText.textContent =
        showOnboardingStatus
          ? t("status.onboarding_ready")
          : snapshot.last_action || t("status.waiting");
    }
    if (elements.reactorState) {
      elements.reactorState.textContent = currentState.toUpperCase();
    }
    if (elements.statusDot) {
      elements.statusDot.classList.toggle("status-pill__dot--active", ["responding", "capturing", "processing"].includes(currentState));
    }

    elements.localModeToggle.checked = Boolean(snapshot.local_mode_enabled);
    elements.voiceBackendName.textContent = snapshot.voice_backend || t("misc.none");
    elements.voiceBackendStatus.textContent = snapshot.voice_status || t("ops.voice_unavailable");
    elements.deviceProfileMeta.textContent = snapshot.verification_state || t("misc.no_verified_actions");
    renderTrustBadge(snapshot.network_status || {});
    renderAuditLog(snapshot.recent_audit_events || [], snapshot.network_status || {});
    renderSchedules(snapshot.scheduled_tasks || []);
    renderProactiveAlerts(snapshot.proactive_alerts || []);
    renderMemoryTimeline(snapshot.memory_timeline || []);
    renderPlatformInfo(snapshot);
    renderKnowledge(snapshot);
    renderOnboarding(snapshot);
    renderFailures(snapshot);
    setActionLock(Boolean(snapshot.action_in_progress) || snapshot.profile_session?.unlocked === false);
    renderList(elements.activityLog, snapshot.action_history || [], (item) => `[${item.category}] ${item.message}`);
    renderPlan(snapshot.active_plan);
    renderModelInfo(snapshot);
    renderThemeState();
    elements.checkMemory.textContent = snapshot.startup_checks?.memory || t("misc.unknown");
    elements.checkCognition.textContent = snapshot.startup_checks?.cognition || snapshot.cognition_backend || t("misc.unknown");
  }

  function renderConversationSlice(snapshot) {
    const shouldAutoFollow = isThreadNearBottom();
    renderThread(snapshot.conversation_turns || [], shouldAutoFollow);
    renderSessionList(snapshot.conversation_turns || [], "");
    if (elements.conversationSearchModal && !elements.conversationSearchModal.classList.contains("hidden")) {
      renderConversationSearch(elements.conversationSearchInput?.value || "");
    }

    if (snapshot.pending_confirmation) {
      elements.confirmationText.textContent = snapshot.pending_confirmation.prompt;
      elements.confirmationBox.classList.remove("hidden");
    } else {
      elements.confirmationBox.classList.add("hidden");
    }
  }

  function renderContextSlice(snapshot) {
    renderContext(snapshot.active_context_summary);
    elements.proactiveReason.textContent = snapshot.proactive_prompt?.reason || t("proactive.no_prompt");
    elements.proactiveText.textContent = snapshot.proactive_prompt?.message || t("proactive.quiet");

    if (snapshot.morning_brief) {
      elements.briefDate.textContent = formatDate(new Date(snapshot.morning_brief.date), { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      elements.focusText.textContent = snapshot.morning_brief.focus_suggestion;
      elements.musicText.textContent = snapshot.morning_brief.music_suggestion || t("brief.no_suggestion");
      renderList(elements.eventsList, snapshot.morning_brief.events, (item) => {
        const date = new Date(item.starts_at);
        return `${formatDate(date, { hour: "2-digit", minute: "2-digit" })} ${item.title}`;
      });
      renderList(elements.tasksList, snapshot.morning_brief.tasks, (item) => item.title);
      renderReminderList(snapshot.morning_brief.reminders || []);
      return;
    }

    elements.briefDate.textContent = t("brief.not_generated");
    elements.focusText.textContent = t("brief.default_focus");
    elements.musicText.textContent = t("brief.no_suggestion");
    renderList(elements.eventsList, snapshot.active_context_summary?.events || [], (item) => {
      const date = new Date(item.starts_at);
      return `${formatDate(date, { hour: "2-digit", minute: "2-digit" })} ${item.title}`;
    });
    renderList(elements.tasksList, snapshot.active_context_summary?.tasks || [], (item) => item.title);
    renderReminderList(snapshot.reminders_due || []);
  }

  function renderSnapshot(snapshot) {
    currentSnapshot = snapshot;
    applyProductPosture(snapshot);
    const slices = [
      ["runtime", () => renderRuntimeSlice(snapshot)],
      ["conversation", () => renderConversationSlice(snapshot)],
      ["context", () => renderContextSlice(snapshot)],
      ["capabilities", () => renderCapabilities(snapshot.capability_status || [])],
      ["receipts", () => renderReceipts(snapshot.last_receipts)],
    ];
    for (const [key, fn] of slices) {
      if (renderKey(snapshot, key) !== lastRenderedKeys[key]) {
        try {
          fn();
        } catch (err) {
          console.error(`[KERN] renderSnapshot: ${key} slice failed`, err);
        }
        lastRenderedKeys[key] = renderKey(snapshot, key);
      }
    }
  }

  function scheduleSnapshotRender(snapshot) {
    pendingSnapshot = snapshot;
    if (snapshotFrameScheduled) {
      return;
    }
    snapshotFrameScheduled = true;
    requestAnimationFrame(() => {
      snapshotFrameScheduled = false;
      if (!pendingSnapshot) {
        return;
      }
      renderSnapshot(pendingSnapshot);
      pendingSnapshot = null;
    });
  }

  function activateUtilityTab(tabName) {
    elements.utilityTabs.forEach((tab) => {
      const isActive = tab.dataset.tab === tabName;
      tab.classList.toggle("is-active", isActive);
      tab.setAttribute("aria-selected", String(isActive));
    });
    elements.utilityPanels.forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.panel === tabName);
      panel.toggleAttribute("inert", panel.dataset.panel !== tabName);
    });
  }

  function activateSettingsSection(sectionName, options = {}) {
    const { behavior = "smooth", scroll = true } = options;
    let activeSection = null;
    elements.settingsSectionNavItems.forEach((item) => {
      const isActive = item.dataset.settingsSectionNav === sectionName;
      item.classList.toggle("is-active", isActive);
      item.setAttribute("aria-current", isActive ? "true" : "false");
      if (isActive) {
        activeSection = [...elements.settingsSections].find((section) => section.dataset.settingsSection === sectionName) || null;
      }
    });
    if (!activeSection) {
      return;
    }
    elements.settingsSections.forEach((section) => {
      const isActive = section.dataset.settingsSection === sectionName;
      section.classList.toggle("is-active", isActive);
      section.toggleAttribute("hidden", !isActive);
    });
    elements.settingsModalTitle.textContent = activeSection.dataset.settingsTitle || t("settings.title");
    if (scroll && elements.settingsContent) {
      elements.settingsContent.scrollTo({ top: 0, behavior });
    }
  }

  function syncSettingsSectionFromScroll() {
    return;
  }

  function refreshSessionFilter() {
    if (currentSnapshot) {
      renderSessionList(currentSnapshot.conversation_turns || [], "");
    }
  }

  function appendLlmToken(token) {
    if (!token) return;
    if (!llmStreamElement) {
      if (elements.composerAssist) {
        elements.composerAssist.textContent = "";
        elements.composerAssist.classList.add("hidden");
        elements.composerAssist.classList.remove("is-error");
        elements.composerAssist.classList.remove("is-success");
        elements.composerAssist.classList.remove("is-warning");
      }

      llmStreamElement = document.createElement("li");
      llmStreamElement.className = "thread-turn thread-turn--assistant thread-turn--streaming";

      const meta = document.createElement("div");
      meta.className = "thread-turn__meta";
      meta.textContent = t("chat.kern");

      const bubble = document.createElement("div");
      bubble.className = "thread-turn__bubble";

      llmStreamBody = document.createElement("div");
      llmStreamBody.className = "thread-turn__body";
      bubble.appendChild(llmStreamBody);

      llmStreamElement.appendChild(meta);
      llmStreamElement.appendChild(bubble);
      elements.threadList.appendChild(llmStreamElement);
    }
    llmStreamBody.textContent += token;
    if (isThreadNearBottom()) {
      scrollThreadToLatest("smooth");
    }
  }

  function finalizeLlmStream(isRag = false) {
    if (llmStreamElement && isRag) {
      llmStreamElement.classList.add("thread-turn--rag");
      const meta = llmStreamElement.querySelector(".thread-turn__meta");
      if (meta) {
        meta.textContent = t("chat.grounded");
      }
    }
    llmStreamElement = null;
    llmStreamBody = null;
  }

  let lastRagQuery = "";

  const escapeHtml = escapeHTML;

  function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function highlightTerms(text, terms) {
    if (!terms.length) return escapeHtml(text);
    const pattern = terms.map(escapeRegex).join("|");
    return escapeHtml(text).replace(
      new RegExp(`(${pattern})`, "gi"),
      '<mark class="passage-highlight">$1</mark>'
    );
  }

  function openPassageViewer(source, query) {
    if (!elements.passageModal) return;
    elements.passageTitle.textContent = source.title || t("docs.untitled");
    elements.passageDocType.textContent =
      source.source_type === "memory" ? t("passage.memory") : t("passage.eyebrow");
    elements.passageScore.textContent = t("passage.relevance", { score: (Number(source.score || 0) * 100).toFixed(0) });

    const text = source.text || "";
    if (query && text) {
      const terms = query.split(/\s+/).filter((t) => t.length >= 3);
      elements.passageBody.innerHTML = highlightTerms(text, terms);
    } else {
      elements.passageBody.textContent = text || t("passage.no_text");
    }

    passageController?.open();
  }

  function renderRagSources(payload) {
    const sources = payload?.sources || [];
    if (!sources.length) return;
    lastRagQuery = payload?.query || "";

    const lastTurn = elements.threadList.lastElementChild;
    if (!lastTurn || !lastTurn.classList.contains("thread-turn--rag")) return;
    const bubble = lastTurn.querySelector(".thread-turn__bubble");
    if (!bubble) return;

    bubble.querySelector(".rag-sources")?.remove();

    const container = document.createElement("div");
    container.className = "rag-sources";

    const label = document.createElement("div");
    label.className = "rag-sources__label";
    label.textContent = t("passage.sources", { count: sources.length });
    container.appendChild(label);

    sources.forEach((src) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "rag-sources__card";
      card.title = t("passage.view");

      const title = document.createElement("span");
      title.className = "rag-sources__title";
      title.textContent = src.title || t("docs.untitled");
      card.appendChild(title);

      const score = document.createElement("span");
      score.className = "rag-sources__score";
      score.textContent = `${(Number(src.score || 0) * 100).toFixed(0)}%`;
      card.appendChild(score);

      const icon = document.createElement("span");
      icon.className = "rag-sources__view-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML =
        '<svg viewBox="0 0 24 24"><path d="M13.5 6H5.25A2.25 2.25 0 0 0 3 8.25v10.5A2.25 2.25 0 0 0 5.25 21h10.5A2.25 2.25 0 0 0 18 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" /></svg>';
      card.appendChild(icon);

      card.addEventListener("click", () => openPassageViewer(src, lastRagQuery));
      container.appendChild(card);
    });

    bubble.appendChild(container);
    if (isThreadNearBottom()) {
      scrollThreadToLatest("smooth");
    }
  }

  function showPanelLoading(containerId, messageKey) {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.innerHTML = `<div class="panel-loading"><span class="kern-spinner"></span>${t(messageKey)}</div>`;
  }

  return {
      updateConnectionState,
      updateClock,
      applySidebarCollapsed,
      autoResizeCommandInput,
      syncConversationState,
    scheduleSnapshotRender,
    activateUtilityTab,
    activateSettingsSection,
    syncSettingsSectionFromScroll,
    refreshSessionFilter,
    renderConversationSearch,
    scrollToTurn,
    renderThemeState,
    appendLlmToken,
    finalizeLlmStream,
    renderRagSources,
    renderMemorySearchResults,
    renderKnowledgeGraph,
    renderKnowledgeGraphSearch,
      showPanelLoading,
        setOnboardingOptimisticState(value) {
          onboardingUiState = value || null;
          renderOnboarding(currentSnapshot || { onboarding: {}, trust_summary: {} });
        },
        clearOnboardingOptimisticState() {
          onboardingUiState = null;
          renderOnboarding(currentSnapshot || { onboarding: {}, trust_summary: {} });
        },
        getCurrentSnapshot() {
          return currentSnapshot;
        },
      setAuditCategoryFilter(value) {
        auditCategoryFilter = String(value || "").trim().toLowerCase();
        renderAuditLog(currentSnapshot?.recent_audit_events || [], currentSnapshot?.network_status || {});
      },
      setConversationPrimed(value) {
        conversationPrimed = Boolean(value);
      },
    getSidebarCollapsedKey() {
      return SIDEBAR_COLLAPSED_KEY;
    },
  };
}
