import { t } from "/static/js/i18n.js?v=20260422k";
import { getCurrentLang } from "/static/js/i18n.js?v=20260422k";
import { escapeHTML } from "/static/js/utils.js?v=20260422k";

const SIDEBAR_COLLAPSED_KEY = "kern.sidebar.collapsed";
const SESSION_PREFS_KEY = "kern.sidebar.sessionPrefs";
const SESSION_HIDDEN_TURNS_KEY = "kern.sidebar.hiddenTurnIds";
const SESSION_ARCHIVE_KEY = "kern.sidebar.archivedSessions";
const SIDEBAR_WORKSPACES_KEY = "kern.sidebar.workspaces";
const SIDEBAR_ACTIVE_WORKSPACE_KEY = "kern.sidebar.workspace.active";
const SIDEBAR_WORKSPACE_HIDDEN_KEY = "kern.sidebar.workspace.hidden";
const SIDEBAR_STATUS_OPEN_KEY = "kern.sidebar.status.open";

function isUntranslatedI18nKey(value, prefix) {
  return typeof value === "string" && (value === prefix || value.startsWith(`${prefix}_`));
}

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
  let sessionActionMenu = null;
  let sessionConfirmToast = null;
  let workspaceConfirmToast = null;
  let workspaceCreateBound = false;
  let activeSessionMenuId = "";
  let selectedArchivedSessionId = "";
  let editingSessionId = "";
  let sessionDraftTitle = "";
  let workspaceDialogMode = "create";
  let editingWorkspaceId = "";
  let lastConnectionState = "connecting";
  let sidebarControlsBound = false;
  const lastRenderedKeys = {
    conversation: "",
    context: "",
    capabilities: "",
    receipts: "",
    runtime: "",
    workspaces: "",
  };

  function setOptionalText(element, value) {
    if (element) {
      element.textContent = value;
    }
  }

  function setOptionalDisabled(element, disabled) {
    if (element) {
      element.disabled = disabled;
    }
  }

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

  function formatDateTime(timestamp) {
    if (!timestamp) return "";
    return formatDate(new Date(timestamp), {
      year: "numeric",
      month: "short",
      day: "numeric",
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

  function readJsonStorage(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  }

  function writeJsonStorage(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // ignore storage failures
    }
  }

  function readStorageFlag(key, fallback = true) {
    try {
      const raw = localStorage.getItem(key);
      if (raw === null) return fallback;
      return raw === "1";
    } catch {
      return fallback;
    }
  }

  function writeStorageFlag(key, value) {
    try {
      localStorage.setItem(key, value ? "1" : "0");
    } catch {
      // ignore storage failures
    }
  }

  function slugifyLabel(value) {
    return String(value || "")
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || `workspace-${Date.now()}`;
  }

  function getSessionPrefs() {
    return readJsonStorage(SESSION_PREFS_KEY, {});
  }

  function setSessionPrefs(prefs) {
    writeJsonStorage(SESSION_PREFS_KEY, prefs);
  }

  function getHiddenTurnIds() {
    return readJsonStorage(SESSION_HIDDEN_TURNS_KEY, []);
  }

  function setHiddenTurnIds(ids) {
    writeJsonStorage(SESSION_HIDDEN_TURNS_KEY, Array.from(new Set(ids.filter(Boolean))));
  }

  function getSessionPref(turnId) {
    return getSessionPrefs()[turnId] || {};
  }

  function updateSessionPref(turnId, updates) {
    if (!turnId) return;
    const prefs = getSessionPrefs();
    prefs[turnId] = {
      ...(prefs[turnId] || {}),
      ...updates,
    };
    setSessionPrefs(prefs);
  }

  function deleteSessionPref(turnId) {
    if (!turnId) return;
    const prefs = getSessionPrefs();
    delete prefs[turnId];
    setSessionPrefs(prefs);
  }

  function getVisibleTurns(turns = []) {
    const hiddenIds = new Set(getHiddenTurnIds());
    return turns.filter((turn) => !hiddenIds.has(turn.id));
  }

  function archivedStorageKey(snapshot = currentSnapshot || {}) {
    const profileSlug = snapshot?.profile_slug || snapshot?.active_profile?.slug || "default";
    return `${SESSION_ARCHIVE_KEY}.${profileSlug}`;
  }

  function getArchivedSessions() {
    return readJsonStorage(archivedStorageKey(), []);
  }

  function setArchivedSessions(sessions) {
    writeJsonStorage(archivedStorageKey(), sessions.slice(0, 30));
  }

  function archiveTurns(turns = currentSnapshot?.conversation_turns || []) {
    const visibleTurns = getVisibleTurns(turns).filter(isRenderableTurn);
    const userTurns = visibleTurns.filter((turn) => turn.role === "user" && String(turn.text || "").trim());
    if (!userTurns.length) {
      return null;
    }
    const firstUserTurn = userTurns[0];
    const latestTurn = visibleTurns[visibleTurns.length - 1] || firstUserTurn;
    const id = firstUserTurn.id || `archived-${Date.now()}`;
    const existing = getArchivedSessions().filter((session) => session.id !== id);
    const archived = {
      id,
      title: String(firstUserTurn.text || t("session.untitled")).trim(),
      createdAt: firstUserTurn.timestamp || new Date().toISOString(),
      updatedAt: latestTurn.timestamp || new Date().toISOString(),
      turns: visibleTurns,
    };
    setArchivedSessions([archived, ...existing]);
    return archived;
  }

  function buildSidebarSessions(turns = []) {
    const visibleTurns = getVisibleTurns(turns);
    const userTurns = visibleTurns.filter((turn) => turn.role === "user" && String(turn.text || "").trim());
    const sessions = [];
    if (visibleTurns.length && userTurns.length) {
      const firstUserTurn = userTurns[0];
      const latestTurn = visibleTurns[visibleTurns.length - 1] || firstUserTurn;
      const sessionId = firstUserTurn.id || "current-conversation";
      const pref = getSessionPref(sessionId);
      sessions.push({
        id: sessionId,
        title: String(pref.title || firstUserTurn.text || t("session.untitled")).trim(),
        pinned: Boolean(pref.pinned),
        current: !selectedArchivedSessionId,
        archived: false,
        turn: latestTurn,
        segment: visibleTurns,
        segmentTurnIds: visibleTurns.map((entry) => entry.id).filter(Boolean),
      });
    }
    getArchivedSessions().forEach((session) => {
      const pref = getSessionPref(session.id);
      const segment = Array.isArray(session.turns) ? session.turns : [];
      const latestTurn = segment[segment.length - 1] || {};
      sessions.push({
        id: session.id,
        title: String(pref.title || session.title || t("session.untitled")).trim(),
        pinned: Boolean(pref.pinned),
        current: selectedArchivedSessionId === session.id,
        archived: true,
        turn: latestTurn,
        segment,
        segmentTurnIds: segment.map((entry) => entry.id).filter(Boolean),
        updatedAt: session.updatedAt,
      });
    });
    return sessions;
  }

  function getStoredWorkspaces() {
    return readJsonStorage(SIDEBAR_WORKSPACES_KEY, []);
  }

  function readCookie(name) {
    const prefix = `${name}=`;
    return document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(prefix))
      ?.slice(prefix.length) || "";
  }

  function csrfHeaders() {
    const token = readCookie("kern_csrf_token");
    return token ? { "x-csrf-token": token } : {};
  }

  async function postWorkspaceJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...csrfHeaders(),
      },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const error = new Error(`Workspace request failed: ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  async function createRemoteWorkspace(workspace) {
    const payload = await postWorkspaceJson("/admin/workspaces", {
      slug: workspace.id,
      title: workspace.title,
    });
    const remote = payload.workspace || payload.item || workspace;
    return {
      id: remote.slug || workspace.id,
      title: remote.title || workspace.title,
      kind: workspace.kind || "custom",
    };
  }

  async function selectRemoteWorkspace(workspace) {
    return postWorkspaceJson("/auth/session/select-workspace", {
      workspace_slug: workspace.id,
    });
  }

  function rememberWorkspace(workspace, options = {}) {
    const items = getStoredWorkspaces().filter((item) => item.id !== workspace.id);
    if (workspace.kind !== "active") {
      items.unshift({
        id: workspace.id,
        title: workspace.title,
        kind: workspace.kind || "custom",
      });
      setStoredWorkspaces(items.slice(0, 7));
    }
    setHiddenWorkspaceIds(getHiddenWorkspaceIds().filter((entry) => entry !== workspace.id));
    try {
      localStorage.setItem(SIDEBAR_ACTIVE_WORKSPACE_KEY, workspace.id);
    } catch {
      // ignore storage failures
    }
    if (options.render !== false) {
      lastRenderedKeys.workspaces = "";
      renderSidebarWorkspaces(currentSnapshot || {});
    }
  }

  async function activateWorkspace(workspace) {
    try {
      await selectRemoteWorkspace(workspace);
    } catch (error) {
      if (error?.status === 404 && workspace.kind === "custom") {
        const created = await createRemoteWorkspace(workspace);
        workspace = { ...workspace, ...created };
        await selectRemoteWorkspace(workspace);
      } else {
        throw error;
      }
    }
    rememberWorkspace(workspace, { render: false });
    window.location.reload();
  }

  function setStoredWorkspaces(items) {
    writeJsonStorage(SIDEBAR_WORKSPACES_KEY, items);
  }

  function getHiddenWorkspaceIds() {
    return readJsonStorage(SIDEBAR_WORKSPACE_HIDDEN_KEY, []);
  }

  function setHiddenWorkspaceIds(ids) {
    writeJsonStorage(
      SIDEBAR_WORKSPACE_HIDDEN_KEY,
      Array.from(new Set((ids || []).filter(Boolean))),
    );
  }

  function getSeedWorkspace(snapshot) {
    const profile = snapshot?.active_profile || {};
    return {
      id: profile.slug || "primary-profile",
      title: profile.title || t("workspace.default"),
      kind: "active",
    };
  }

  function getSidebarWorkspaces(snapshot) {
    const seed = getSeedWorkspace(snapshot);
    const hiddenIds = new Set(getHiddenWorkspaceIds());
    const stored = getStoredWorkspaces().filter((item) => item && item.id && item.title);
    const merged = [{ ...seed }];
    stored.forEach((item) => {
      const existing = merged.find((entry) => entry.id === item.id);
      if (existing) {
        existing.title = item.title;
        existing.kind = item.kind || existing.kind;
      } else {
        merged.push({
          id: item.id,
          title: item.title,
          kind: item.kind || "custom",
        });
      }
    });
    const visible = merged.filter((item) => !hiddenIds.has(item.id));
    return visible.slice(0, 8);
  }

  function getActiveWorkspaceId(snapshot) {
    const available = getSidebarWorkspaces(snapshot);
    const preferred = localStorage.getItem(SIDEBAR_ACTIVE_WORKSPACE_KEY) || available[0]?.id || "";
    return available.some((item) => item.id === preferred) ? preferred : available[0]?.id || "";
  }

  function renderSidebarWorkspaces(snapshot) {
    if (!elements.workspaceList) {
      return;
    }
    const workspaces = getSidebarWorkspaces(snapshot);
    const activeWorkspaceId = getActiveWorkspaceId(snapshot);
    const workspaceRenderKey = JSON.stringify({
      activeWorkspaceId,
      workspaces: workspaces.map((workspace) => ({
        id: workspace.id,
        title: workspace.title,
      })),
    });
    if (workspaceRenderKey === lastRenderedKeys.workspaces) {
      return;
    }
    lastRenderedKeys.workspaces = workspaceRenderKey;
    elements.workspaceList.innerHTML = "";

    if (!workspaces.length) {
      const emptyItem = document.createElement("li");
      emptyItem.className = "sidebar-list__empty";
      emptyItem.textContent = t("workspace.none");
      elements.workspaceList.appendChild(emptyItem);
      return;
    }

    workspaces.forEach((workspace) => {
      const li = document.createElement("li");
      const row = document.createElement("div");
      row.className = "sidebar-workspace-item";
      if (workspace.id === activeWorkspaceId) {
        row.classList.add("is-active");
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sidebar-workspace-item__main";
      button.dataset.workspaceId = workspace.id;
      button.innerHTML = `
        <span class="sidebar-workspace-item__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M4.75 9.25c0-1.768 0-2.652.549-3.201C5.848 5.5 6.732 5.5 8.5 5.5h2.017c.563 0 .844 0 1.101.087.226.076.433.198.608.358.198.182.338.423.618.904l.237.407c.16.274.24.411.362.513.108.09.231.16.364.207.149.052.31.052.631.052H16c1.768 0 2.652 0 3.201.549.549.549.549 1.433.549 3.201v1.482c0 1.768 0 2.652-.549 3.201C18.652 17 17.768 17 16 17H8.5c-1.768 0-2.652 0-3.201-.549-.549-.549-.549-1.433-.549-3.201V9.25Z"/><path d="M8 5.5V4.75C8 4.06 8.56 3.5 9.25 3.5h2.05c.44 0 .66 0 .86.072.177.063.338.164.473.295.153.149.26.342.474.727l.393.706"/></svg>
        </span>
        <span class="sidebar-workspace-item__label">${escapeHtml(workspace.title)}</span>
      `;
      button.addEventListener("click", async () => {
        if (workspace.id === activeWorkspaceId) {
          return;
        }
        button.disabled = true;
        try {
          await activateWorkspace(workspace);
        } catch (error) {
          button.disabled = false;
          console.warn("Workspace switch failed", error);
        }
      });
      const actions = document.createElement("div");
      actions.className = "sidebar-workspace-item__actions";

      const renameButton = document.createElement("button");
      renameButton.type = "button";
      renameButton.className = "sidebar-workspace-item__action";
      renameButton.setAttribute("aria-label", t("workspace.rename"));
      renameButton.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l10.5-10.5a2.121 2.121 0 1 0-3-3L5.5 17v3Z"/><path d="M13.5 6.5l4 4"/></svg>`;
      renameButton.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openWorkspaceCreateModal({ mode: "rename", workspace });
      });

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "sidebar-workspace-item__action sidebar-workspace-item__action--danger";
      deleteButton.setAttribute("aria-label", t("workspace.delete"));
      deleteButton.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg>`;
      deleteButton.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openWorkspaceDeleteToast(workspace);
      });

      actions.append(renameButton, deleteButton);
      row.append(button, actions);
      li.appendChild(row);
      elements.workspaceList.appendChild(li);
    });
  }

  function formatSidebarSyncStatus(snapshot) {
    const syncTargets = snapshot?.sync_targets || [];
    const timestamps = syncTargets
      .map((target) => target?.last_sync_at)
      .filter(Boolean)
      .map((value) => new Date(value))
      .filter((value) => !Number.isNaN(value.getTime()))
      .sort((left, right) => right.getTime() - left.getTime());
    if (!timestamps.length) {
      return t("sidebar.system.last_sync_now");
    }
    return t("sidebar.system.last_sync_at", {
      time: formatDate(timestamps[0], { hour: "2-digit", minute: "2-digit" }),
    });
  }

  function renderSidebarSystemStatus(snapshot) {
    if (!elements.systemStatusCard || !elements.systemStatusToggle) {
      return;
    }
    const hasRuntimeIssues = Array.isArray(snapshot?.runtime_degraded_reasons) && snapshot.runtime_degraded_reasons.length > 0;
    const hasSyncIssues = (snapshot?.sync_targets || []).some((target) => target.status === "degraded");
    const isOperational = lastConnectionState === "connected" && !hasRuntimeIssues && !hasSyncIssues;
    const isOpen = readStorageFlag(SIDEBAR_STATUS_OPEN_KEY, true);
    elements.systemStatusCard.dataset.open = isOpen ? "true" : "false";
    elements.systemStatusToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    elements.systemStatusLabel.textContent = isOperational ? t("sidebar.system.operational") : t("sidebar.system.attention");
    elements.systemStatusDetail.textContent = formatSidebarSyncStatus(snapshot);
    elements.systemStatusDot.dataset.state = isOperational ? "healthy" : "warning";
  }

  function ensureSidebarShellControls() {
    if (sidebarControlsBound) {
      return;
    }
    sidebarControlsBound = true;
    elements.newWorkspaceButton?.addEventListener("click", () => {
      openWorkspaceCreateModal();
    });
  }

  function ensureWorkspaceCreateModal() {
    if (workspaceCreateBound || !elements.workspaceCreateModal) {
      return;
    }
    workspaceCreateBound = true;
    const close = () => closeWorkspaceCreateModal();
    const submit = () => submitWorkspaceCreateModal();
    elements.workspaceCreateModal.addEventListener("click", (event) => {
      const dialog = event.target instanceof Element ? event.target.closest(".workspace-create-dialog") : null;
      if (!dialog) {
        close();
      }
    });
    elements.workspaceCreateCancel?.addEventListener("click", close);
    elements.workspaceCreateConfirm?.addEventListener("click", submit);
    elements.workspaceCreateInput?.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
  }

  function openWorkspaceCreateModal(options = {}) {
    ensureWorkspaceCreateModal();
    if (!elements.workspaceCreateModal) {
      return;
    }
    const workspace = options.workspace || null;
    workspaceDialogMode = options.mode === "rename" ? "rename" : "create";
    editingWorkspaceId = workspace?.id || "";
    const titleKey = workspaceDialogMode === "rename" ? "workspace.rename_title" : "workspace.create_title";
    const promptKey = workspaceDialogMode === "rename" ? "workspace.rename_prompt" : "workspace.create_prompt";
    const confirmKey = workspaceDialogMode === "rename" ? "confirm.save" : "confirm.create";
    const title = t(titleKey);
    const prompt = t(promptKey);
    const confirm = t(confirmKey);
    if (elements.workspaceCreateTitle) {
      elements.workspaceCreateTitle.dataset.i18n = titleKey;
      elements.workspaceCreateTitle.textContent = title;
    }
    if (elements.workspaceCreateConfirm) {
      elements.workspaceCreateConfirm.dataset.i18n = confirmKey;
      elements.workspaceCreateConfirm.textContent = confirm;
    }
    if (elements.workspaceCreateInput) {
      elements.workspaceCreateInput.value = workspaceDialogMode === "rename" ? workspace?.title || "" : "";
      elements.workspaceCreateInput.dataset.i18nPlaceholder = promptKey;
      elements.workspaceCreateInput.placeholder = prompt;
      elements.workspaceCreateInput.setAttribute("aria-label", title);
    }
    elements.workspaceCreateModal.classList.remove("hidden");
    elements.workspaceCreateModal.setAttribute("aria-hidden", "false");
    requestAnimationFrame(() => {
      elements.workspaceCreateModal.classList.add("is-open");
      elements.workspaceCreateInput?.focus();
    });
  }

  function closeWorkspaceCreateModal() {
    if (!elements.workspaceCreateModal) {
      return;
    }
    workspaceDialogMode = "create";
    editingWorkspaceId = "";
    elements.workspaceCreateModal.classList.remove("is-open");
    elements.workspaceCreateModal.setAttribute("aria-hidden", "true");
    window.setTimeout(() => {
      if (!elements.workspaceCreateModal.classList.contains("is-open")) {
        elements.workspaceCreateModal.classList.add("hidden");
      }
    }, 180);
  }

  async function submitWorkspaceCreateModal() {
    const trimmed = String(elements.workspaceCreateInput?.value || "").trim();
    if (!trimmed) {
      elements.workspaceCreateInput?.focus();
      return;
    }
    const items = getStoredWorkspaces();
    const id = workspaceDialogMode === "rename" && editingWorkspaceId ? editingWorkspaceId : slugifyLabel(trimmed);
    const existingIndex = items.findIndex((item) => item.id === id);
    const existingSnapshotWorkspace =
      getSidebarWorkspaces(currentSnapshot || {}).find((item) => item.id === id) ||
      getSidebarWorkspaces({}).find((item) => item.id === id);
    const nextItem = {
      id,
      title: trimmed,
      kind: existingIndex >= 0 ? items[existingIndex].kind || "custom" : existingSnapshotWorkspace?.kind || "custom",
    };
    if (existingIndex >= 0) {
      items.splice(existingIndex, 1);
    }
    if (workspaceDialogMode === "rename") {
      items.unshift(nextItem);
      setStoredWorkspaces(items.slice(0, 7));
      rememberWorkspace(nextItem);
      closeWorkspaceCreateModal();
      return;
    }
    elements.workspaceCreateConfirm.disabled = true;
    try {
      const remoteWorkspace = await createRemoteWorkspace(nextItem);
      rememberWorkspace(remoteWorkspace, { render: false });
      await selectRemoteWorkspace(remoteWorkspace);
      closeWorkspaceCreateModal();
      window.location.reload();
    } catch (error) {
      console.warn("Workspace create failed", error);
      elements.workspaceCreateInput?.focus();
    } finally {
      elements.workspaceCreateConfirm.disabled = false;
    }
  }

  function ensureWorkspaceDeleteToast() {
    if (workspaceConfirmToast) {
      return workspaceConfirmToast;
    }
    workspaceConfirmToast = document.createElement("div");
    workspaceConfirmToast.className = "session-confirm-toast hidden";
    workspaceConfirmToast.innerHTML = `
      <div class="session-confirm-toast__backdrop"></div>
      <div class="session-confirm-toast__dialog" role="alertdialog" aria-modal="true">
        <div class="session-confirm-toast__icon">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg>
        </div>
        <div class="session-confirm-toast__copy">
          <strong id="workspaceConfirmTitle">${t("workspace.delete_confirm_title")}</strong>
          <p id="workspaceConfirmBody">${t("workspace.delete_confirm_body")}</p>
        </div>
        <div class="session-confirm-toast__actions">
          <button type="button" class="ghost-button" data-workspace-confirm="cancel">${t("confirm.cancel")}</button>
          <button type="button" class="solid-button" data-workspace-confirm="delete">${t("workspace.delete")}</button>
        </div>
      </div>
    `;
    workspaceConfirmToast.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-workspace-confirm]") : null;
      if (button) {
        const action = button.getAttribute("data-workspace-confirm");
        const workspaceId = workspaceConfirmToast.dataset.workspaceId || "";
        if (action === "delete" && workspaceId) {
          const items = getStoredWorkspaces().filter((item) => item.id !== workspaceId);
          setStoredWorkspaces(items);
          const visible = getHiddenWorkspaceIds();
          if (!visible.includes(workspaceId)) {
            visible.push(workspaceId);
          }
          setHiddenWorkspaceIds(visible);
          if ((localStorage.getItem(SIDEBAR_ACTIVE_WORKSPACE_KEY) || "") === workspaceId) {
            try {
              localStorage.removeItem(SIDEBAR_ACTIVE_WORKSPACE_KEY);
            } catch {
              // ignore storage failures
            }
          }
          renderSidebarWorkspaces(currentSnapshot || {});
        }
        closeWorkspaceDeleteToast();
        return;
      }
      if (event.target instanceof Element && event.target.classList.contains("session-confirm-toast__backdrop")) {
        closeWorkspaceDeleteToast();
      }
    });
    document.body.appendChild(workspaceConfirmToast);
    return workspaceConfirmToast;
  }

  function openWorkspaceDeleteToast(workspace) {
    const toast = ensureWorkspaceDeleteToast();
    toast.dataset.workspaceId = workspace.id;
    const title = workspace?.title || t("workspace.default");
    const body = t("workspace.delete_confirm_body").replace("${title}", title);
    toast.querySelector("#workspaceConfirmBody").textContent = body;
    toast.classList.remove("hidden");
    requestAnimationFrame(() => {
      toast.classList.add("is-open");
    });
  }

  function closeWorkspaceDeleteToast() {
    workspaceConfirmToast?.classList.remove("is-open");
    window.setTimeout(() => {
      if (workspaceConfirmToast && !workspaceConfirmToast.classList.contains("is-open")) {
        workspaceConfirmToast.classList.add("hidden");
      }
    }, 180);
    if (workspaceConfirmToast) {
      workspaceConfirmToast.dataset.workspaceId = "";
    }
  }

  function closeSessionMenu() {
    activeSessionMenuId = "";
    if (!sessionActionMenu) {
      return;
    }
    sessionActionMenu.classList.remove("is-open");
    window.setTimeout(() => {
      if (!sessionActionMenu.classList.contains("is-open")) {
        sessionActionMenu.classList.add("hidden");
      }
    }, 180);
  }

  function ensureSessionActionMenu() {
    if (sessionActionMenu) {
      return sessionActionMenu;
    }
    sessionActionMenu = document.createElement("div");
    sessionActionMenu.className = "session-action-menu hidden";
    sessionActionMenu.innerHTML = `
      <button type="button" class="session-action-menu__item" data-session-action="rename">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l10.5-10.5a2.121 2.121 0 1 0-3-3L5.5 17v3Z"/><path d="M13.5 6.5l4 4"/></svg>
        <span>${labelText("session.rename", "Rename", "Umbenennen")}</span>
      </button>
      <button type="button" class="session-action-menu__item" data-session-action="pin">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4l6 6"/><path d="M16 2l6 6"/><path d="M9 15 3 21"/><path d="m5 13 6 6"/><path d="M11 17l7-7-4-4-7 7"/></svg>
        <span>${labelText("session.pin", "Pin", "Anheften")}</span>
      </button>
      <button type="button" class="session-action-menu__item session-action-menu__item--danger" data-session-action="delete">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg>
        <span>${labelText("session.delete", "Delete", "Löschen")}</span>
      </button>
    `;
    sessionActionMenu.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-session-action]") : null;
      if (!button || !activeSessionMenuId) {
        return;
      }
      const action = button.getAttribute("data-session-action");
      const turnId = activeSessionMenuId;
      closeSessionMenu();
      if (action === "rename") {
        editingSessionId = turnId;
        sessionDraftTitle = getSessionPref(turnId).title || "";
        refreshSessionFilter();
      } else if (action === "pin") {
        const pref = getSessionPref(turnId);
        updateSessionPref(turnId, { pinned: !pref.pinned });
        refreshSessionFilter();
      } else if (action === "delete") {
        openSessionDeleteToast(turnId);
      }
    });
    document.body.appendChild(sessionActionMenu);
    return sessionActionMenu;
  }

  function positionSessionMenu(trigger) {
    const menu = ensureSessionActionMenu();
    const rect = trigger.getBoundingClientRect();
    menu.style.left = `${Math.min(window.innerWidth - 212, Math.max(12, rect.right - 196))}px`;
    menu.style.top = `${Math.min(window.innerHeight - 168, rect.bottom + 8)}px`;
    menu.classList.remove("hidden");
    requestAnimationFrame(() => {
      menu.classList.add("is-open");
    });
  }

  function ensureSessionConfirmToast() {
    if (sessionConfirmToast) {
      return sessionConfirmToast;
    }
    sessionConfirmToast = document.createElement("div");
    sessionConfirmToast.className = "session-confirm-toast hidden";
    sessionConfirmToast.innerHTML = `
      <div class="session-confirm-toast__backdrop"></div>
      <div class="session-confirm-toast__dialog" role="alertdialog" aria-modal="true">
        <div class="session-confirm-toast__icon">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg>
        </div>
        <div class="session-confirm-toast__copy">
          <strong id="sessionConfirmTitle">${labelText("session.delete_confirm_title", "Delete conversation?", "Gespräch löschen?")}</strong>
          <p id="sessionConfirmBody">${labelText("session.delete_confirm_body", "\"${title}\" will be removed from the sidebar.", "\"${title}\" wird aus dieser Seitenleiste entfernt.")}</p>
        </div>
        <div class="session-confirm-toast__actions">
          <button type="button" class="ghost-button" data-session-confirm="cancel">${labelText("confirm.cancel", "Cancel", "Abbrechen")}</button>
          <button type="button" class="solid-button" data-session-confirm="delete">${labelText("session.delete", "Delete", "Löschen")}</button>
        </div>
      </div>
    `;
    sessionConfirmToast.addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("[data-session-confirm]") : null;
      if (button) {
        const action = button.getAttribute("data-session-confirm");
        const turnId = sessionConfirmToast.dataset.turnId || "";
        if (action === "delete" && turnId) {
          const session = buildSidebarSessions(currentSnapshot?.conversation_turns || []).find((entry) => entry.id === turnId);
          if (session) {
            if (session.archived) {
              setArchivedSessions(getArchivedSessions().filter((entry) => entry.id !== turnId));
              if (selectedArchivedSessionId === turnId) {
                selectedArchivedSessionId = "";
                renderThread(currentSnapshot?.conversation_turns || [], true);
              }
            } else {
              setHiddenTurnIds([...getHiddenTurnIds(), ...session.segmentTurnIds]);
            }
            if (editingSessionId === turnId) {
              editingSessionId = "";
              sessionDraftTitle = "";
            }
            deleteSessionPref(turnId);
            queueSnapshotRender(currentSnapshot);
          }
        }
        closeSessionDeleteToast();
        return;
      }
      if (event.target instanceof Element && event.target.classList.contains("session-confirm-toast__backdrop")) {
        closeSessionDeleteToast();
      }
    });
    document.body.appendChild(sessionConfirmToast);
    return sessionConfirmToast;
  }

  function openSessionDeleteToast(turnId) {
    const toast = ensureSessionConfirmToast();
    const session = buildSidebarSessions(currentSnapshot?.conversation_turns || []).find((entry) => entry.id === turnId);
    toast.dataset.turnId = turnId;
    const title = session?.title || labelText("session.untitled", "Untitled conversation", "Unbenanntes Gespräch");
    toast.querySelector("#sessionConfirmBody").textContent = labelText(
      "session.delete_confirm_body",
      "\"${title}\" will be removed from the sidebar.",
      "\"${title}\" wird aus dieser Seitenleiste entfernt.",
    ).replace("${title}", title);
    toast.classList.remove("hidden");
    requestAnimationFrame(() => {
      toast.classList.add("is-open");
    });
  }

  function closeSessionDeleteToast() {
    sessionConfirmToast?.classList.remove("is-open");
    window.setTimeout(() => {
      if (sessionConfirmToast && !sessionConfirmToast.classList.contains("is-open")) {
        sessionConfirmToast.classList.add("hidden");
      }
    }, 180);
    if (sessionConfirmToast) {
      sessionConfirmToast.dataset.turnId = "";
    }
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

  function uiText(deText, enText) {
    return (document.documentElement.lang || "en").toLowerCase().startsWith("de") ? deText : enText;
  }

  function labelText(key, enText, deText = enText) {
    const value = t(key);
    return value && value !== key ? value : uiText(deText, enText);
  }

  function humanizeValue(value, fallback = null) {
    if (value == null || value === "") {
      return fallback ?? uiText("Nicht verfügbar", "Not available");
    }
    return titleCase(String(value).replace(/[_-]+/g, " "));
  }

  function shortSettingPath(value, fallback = null) {
    if (!value) return fallback ?? t("settings.not_configured");
    const parts = String(value).split(/[\\/]+/).filter(Boolean);
    if (parts.length <= 2) return String(value);
    return `.../${parts.slice(-2).join("/")}`;
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
    lastConnectionState = state || "connecting";
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
    if (!elements.topTimestamp) {
      return;
    }
    const now = new Date();
    elements.topTimestamp.textContent = formatDate(now, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) {
      return;
    }
    if (sessionActionMenu && !sessionActionMenu.classList.contains("hidden")) {
      const insideMenu = target.closest(".session-action-menu");
      const trigger = target.closest(".sidebar-session-item__menu");
      if (!insideMenu && !trigger) {
        closeSessionMenu();
      }
    }
  });

  function applySidebarCollapsed(collapsed) {
    elements.workspaceShell.classList.toggle("sidebar-collapsed", collapsed);
    elements.sidebarToggle.setAttribute("aria-label", collapsed ? t("nav.expand_sidebar") : t("nav.collapse_sidebar"));
    elements.sidebarToggle.title = collapsed ? t("nav.expand_sidebar") : t("nav.collapse_sidebar");
    if (!collapsed) {
      elements.collapsedSessionsFlyout?.classList.add("hidden");
      elements.collapsedSessionsButton?.setAttribute("aria-expanded", "false");
    }
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
  }

  function renderEmptyHeadingVariant() {
    if (!elements.emptyHeading) {
      return;
    }
    const candidates = [
      t("empty.heading"),
      t("empty.heading_alt_1"),
      t("empty.heading_alt_2"),
      t("empty.heading_alt_3"),
    ].filter((value) => value && !isUntranslatedI18nKey(String(value), "empty.heading"));
    const index = new Date().getDate() % candidates.length;
    const fallbackHeading = "Ready to Help.";
    elements.emptyHeading.textContent = candidates[index] || t("empty.heading") || fallbackHeading;
  }

  function autoResizeCommandInput() {
    elements.commandInput.style.height = "0px";
    elements.commandInput.style.height = `${Math.min(elements.commandInput.scrollHeight, 136)}px`;
  }

  function isRenderableTurn(turn) {
    if (!turn) return false;
    if (turn.kind !== "message" && turn.status === "complete") return false;
    if (String(turn.text || "").trim()) return true;
    if (turn.kind !== "message" || turn.status !== "complete") return true;
    if (Array.isArray(turn.meta?.attachments) && turn.meta.attachments.some(Boolean)) return true;
    if (Array.isArray(turn.meta?.sources) && turn.meta.sources.some(Boolean)) return true;
    return false;
  }

  function syncConversationState(turns = currentSnapshot?.conversation_turns || []) {
    const hasTurns = turns.some(isRenderableTurn);
    const isBusy = Boolean(currentSnapshot?.action_in_progress);
    const engaged = conversationPrimed || hasTurns || isBusy;
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

  function renderBusinessDocuments(documents) {
    if (!elements.businessDocsList) return;
    elements.businessDocsList.innerHTML = "";
    if (!documents || documents.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = t("business.none");
      elements.businessDocsList.appendChild(empty);
      return;
    }
    documents.forEach((doc) => {
      const li = document.createElement("li");
      li.className = "detail-list__item";
      const summary = document.createElement("span");
      summary.textContent = `${doc.title} / ${doc.kind} / ${doc.status}`;
      li.appendChild(summary);
      if (doc.id) {
        const link = document.createElement("a");
        link.href = `/business-documents/${encodeURIComponent(doc.id)}/download`;
        link.textContent = t("business.download");
        link.className = "inline-action-link";
        link.style.marginLeft = "0.75rem";
        li.appendChild(link);
      }
      elements.businessDocsList.appendChild(li);
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
      const placeholder = t("composer.placeholder");
      elements.commandInput.placeholder = isUntranslatedI18nKey(String(placeholder), "composer.placeholder")
        ? "What do you want to work on?"
        : placeholder;
    }
    renderEmptyHeadingVariant();
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
    const readMode = String(doc.document_read_mode || "").trim().toLowerCase();
    if (readMode === "ocr") {
      parts.push(t("docs.read_mode_ocr"));
    } else if (readMode === "hybrid") {
      parts.push(t("docs.read_mode_mixed"));
    } else if (readMode === "vision_assisted") {
      parts.push(t("docs.read_mode_vision_assisted"));
    } else if (readMode === "vision_primary") {
      parts.push(t("docs.read_mode_vision"));
    } else if (readMode === "native") {
      parts.push(t("docs.read_mode_text"));
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
      if (snapshot?.profile_session?.unlocked === false) {
        elements.onboardingModal.classList.add("hidden");
        elements.onboardingModal.classList.remove("is-open");
        elements.onboardingModal.setAttribute("aria-hidden", "true");
        elements.onboardingCard.classList.add("hidden");
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
      elements.onboardingModal.classList.toggle("is-open", isActive);
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
        if (snapshot?.profile_session?.unlocked === false) {
          elements.failurePanel.classList.add("hidden");
          elements.failureList.innerHTML = "";
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
      retry.className = "ghost-button schedule-retry-btn";
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
        item.dataset.testid = "schedule-row";
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

    const bubble = document.createElement("div");
    bubble.className = "thread-turn__bubble";

    const showTechnicalBadge = turn.status && turn.status !== "complete" && turn.kind !== "tool";
    if (showTechnicalBadge) {
      const badge = document.createElement("div");
      badge.className = "thread-turn__badge";
      badge.dataset.kind = turn.kind;
      badge.dataset.status = turn.status;
      badge.textContent = `${turn.kind.replace(/_/g, " ")} - ${turn.status}`;
      bubble.appendChild(badge);
    }

    const attachments = Array.isArray(turn.meta?.attachments) ? turn.meta.attachments.filter(Boolean) : [];
    if (attachments.length) {
      const attachmentList = document.createElement("div");
      attachmentList.className = "thread-turn__attachments";
      attachments.slice(0, 3).forEach((item) => {
        const card = document.createElement("div");
        card.className = "thread-turn__attachment composer-upload-chip composer-upload-chip--indexed";

        const badge = document.createElement("span");
        badge.className = "composer-upload-chip__badge";
        badge.setAttribute("aria-hidden", "true");

        const content = document.createElement("span");
        content.className = "composer-upload-chip__content";

        const label = document.createElement("span");
        label.className = "composer-upload-chip__label";
        label.textContent = item.title || t("docs.untitled");

        const detail = document.createElement("span");
        detail.className = "composer-upload-chip__detail";
        detail.textContent = String(item.file_type || "file").toUpperCase();

        content.appendChild(label);
        content.appendChild(detail);
        card.appendChild(badge);
        card.appendChild(content);
        attachmentList.appendChild(card);
      });
      bubble.appendChild(attachmentList);
    }

    const body = document.createElement("div");
    body.className = "thread-turn__body";
    body.textContent = turn.text;
    bubble.appendChild(body);

    const sources = Array.isArray(turn.meta?.sources) ? turn.meta.sources.filter(Boolean) : [];
    if (sources.length) {
      const sourcesWrap = document.createElement("div");
      sourcesWrap.className = "thread-turn__sources";

      const label = document.createElement("span");
      label.className = "thread-turn__sources-label";
      label.textContent = t("chat.sources");
      sourcesWrap.appendChild(label);

      sources.slice(0, 3).forEach((item) => {
        const pill = document.createElement("span");
        pill.className = "thread-turn__source-pill";
        const chunks = Array.isArray(item.chunk_indexes) && item.chunk_indexes.length
          ? item.chunk_indexes.join(", ")
          : "";
        const pageReadModes = Array.isArray(item.page_read_modes) ? item.page_read_modes.filter(Boolean) : [];
        const readMode = String((pageReadModes[0] || item.document_read_mode || "")).trim().toLowerCase();
        const readModeLabel = readMode === "ocr"
          ? t("docs.read_mode_ocr")
          : readMode === "hybrid"
            ? t("docs.read_mode_mixed")
            : readMode === "vision_assisted"
              ? t("docs.read_mode_vision_assisted")
              : readMode === "vision_primary"
                ? t("docs.read_mode_vision")
                : "";
        pill.textContent = item.title || t("docs.untitled");
        if (readModeLabel) {
          const badge = document.createElement("span");
          badge.className = "thread-turn__source-pill-badge";
          badge.textContent = readModeLabel;
          pill.appendChild(badge);
        }
        if (chunks) {
          pill.title = `${t("chat.source_sections")} ${chunks}${readModeLabel ? ` / ${readModeLabel}` : ""}`;
        } else if (readModeLabel) {
          pill.title = readModeLabel;
        }
        sourcesWrap.appendChild(pill);
      });
      bubble.appendChild(sourcesWrap);
    }

    li.appendChild(bubble);
    return li;
  }

  function renderThread(turns, shouldAutoFollow = true) {
    elements.threadList.innerHTML = "";
    const items = getVisibleTurns(turns || []);
    const renderableItems = items.filter(isRenderableTurn);
    const assistantBusy = Boolean(currentSnapshot?.action_in_progress)
      || ["processing", "responding"].includes(currentSnapshot?.assistant_state || "");
    const lastRenderableTurn = renderableItems[renderableItems.length - 1];
    if (assistantBusy && lastRenderableTurn?.role === "user") {
      renderableItems.push({
        id: "assistant-pending",
        role: "assistant",
        kind: "status",
        status: "pending",
        text: getCurrentLang() === "de" ? "KERN arbeitet daran..." : "KERN is thinking...",
      });
    }
    elements.emptyState.classList.toggle("hidden", renderableItems.length > 0);
    syncConversationState(renderableItems);

    renderableItems.forEach((turn) => {
      elements.threadList.appendChild(createTurnElement(turn));
    });

    const conversationKey = renderableItems.map((turn) => turn.id).join("|");
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
    const userTurns = items.filter((turn) => turn.role === "user" && String(turn.text || "").trim());
    const emptyStateLabel = (() => {
      const value = t("session.no_items");
      return String(value).startsWith("session.")
        ? (getCurrentLang() === "de" ? "Noch nichts im Verlauf." : "No recent chats yet.")
        : value;
    })();

    if (items.length === 0) {
      const empty = document.createElement("li");
      empty.className = "sidebar-list__empty";
      empty.textContent = emptyStateLabel;
      elements.sessionList.appendChild(empty);
      return;
    }

    const createSessionButton = (title, meta, onClick, { time = "", current = false } = {}) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sidebar-session-item";
      if (current) {
        button.classList.add("is-current");
      }

      const marker = document.createElement("span");
      marker.className = "sidebar-session-item__marker";
      marker.setAttribute("aria-hidden", "true");

      const body = document.createElement("span");
      body.className = "sidebar-session-item__body";

      const strong = document.createElement("strong");
      strong.className = "sidebar-session-item__title";
      strong.textContent = title;

      const small = document.createElement("small");
      small.className = "sidebar-session-item__meta";
      small.textContent = meta;

      body.appendChild(strong);
      body.appendChild(small);

      button.appendChild(marker);
      button.appendChild(body);

      if (time) {
        const age = document.createElement("span");
        age.className = "sidebar-session-item__age";
        age.textContent = time;
        button.appendChild(age);
      }

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
        empty.className = "sidebar-list__empty";
        empty.className = "sidebar-list__empty";
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

    if (!userTurns.length) {
      const empty = document.createElement("li");
      empty.className = "sidebar-list__empty";
      empty.textContent = t("session.no_items");
      elements.sessionList.appendChild(empty);
      return;
    }

    userTurns
      .slice(-6)
      .reverse()
      .forEach((turn, index) => {
        const li = document.createElement("li");
        li.appendChild(
          createSessionButton(
            truncate(turn.text, 52),
            index === 0 ? t("session.current") : t("chat.you"),
            () => {
              if (turn.id) {
                scrollToTurn(turn.id);
              } else {
                scrollThreadToLatest("smooth");
              }
              syncConversationState(items);
              requestAnimationFrame(() => {
                elements.commandInput.focus();
              });
            },
            { time: turn.timestamp ? formatTimestamp(turn.timestamp) : "", current: index === 0 }
          )
        );
        elements.sessionList.appendChild(li);
      });
    return;

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

  function renderSidebarSessionList(turns, filterText = "") {
    elements.sessionList.innerHTML = "";
    if (elements.collapsedSessionsList) {
      elements.collapsedSessionsList.innerHTML = "";
    }
    const items = getVisibleTurns(turns || []);
    const normalizedFilter = filterText.trim().toLowerCase();
    const sessions = buildSidebarSessions(items);

    if (sessions.length === 0) {
      const empty = document.createElement("li");
      empty.className = "sidebar-list__empty";
      empty.textContent = t("session.no_items");
      elements.sessionList.appendChild(empty);
      if (elements.collapsedSessionsList) {
        const collapsedEmpty = document.createElement("li");
        collapsedEmpty.className = "sidebar-collapsed-sessions__empty";
        collapsedEmpty.textContent = t("session.no_items");
        elements.collapsedSessionsList.appendChild(collapsedEmpty);
      }
      return;
    }

    const createSessionButton = (session, onClick) => {
      const row = document.createElement("div");
      row.className = "sidebar-session-item";
      row.dataset.sessionId = session.id;
      if (session.current) {
        row.classList.add("is-current");
      }

      const main = document.createElement("button");
      main.type = "button";
      main.className = "sidebar-session-item__main";
      main.dataset.sessionId = session.id;

      const body = document.createElement("span");
      body.className = "sidebar-session-item__body";

      if (editingSessionId === session.id) {
        const input = document.createElement("input");
        input.type = "text";
        input.className = "sidebar-session-item__input";
        input.value = sessionDraftTitle || session.title;
        input.maxLength = 120;
        input.setAttribute("aria-label", t("session.rename"));
        input.addEventListener("click", (event) => event.stopPropagation());
        input.addEventListener("input", () => {
          sessionDraftTitle = input.value;
        });
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            const nextTitle = input.value.trim();
            if (nextTitle) {
              updateSessionPref(session.id, { title: nextTitle });
            } else {
              updateSessionPref(session.id, { title: "" });
            }
            editingSessionId = "";
            sessionDraftTitle = "";
            refreshSessionFilter();
          } else if (event.key === "Escape") {
            event.preventDefault();
            editingSessionId = "";
            sessionDraftTitle = "";
            refreshSessionFilter();
          }
        });
        requestAnimationFrame(() => {
          input.focus();
          input.select();
        });
        body.appendChild(input);
      } else {
        const titleRow = document.createElement("span");
        titleRow.className = "sidebar-session-item__title-row";
        const titleNode = document.createElement("strong");
        titleNode.className = "sidebar-session-item__title";
        titleNode.textContent = truncate(session.title, 58);
        titleRow.appendChild(titleNode);
        if (session.pinned) {
          const pin = document.createElement("span");
          pin.className = "sidebar-session-item__pin";
          pin.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4l6 6"/><path d="M16 2l6 6"/><path d="M9 15 3 21"/><path d="m5 13 6 6"/><path d="M11 17l7-7-4-4-7 7"/></svg>`;
          titleRow.appendChild(pin);
        }
        body.appendChild(titleRow);
      }

      main.appendChild(body);
      main.addEventListener("click", onClick);
      row.appendChild(main);

      const menuTrigger = document.createElement("button");
      menuTrigger.type = "button";
      menuTrigger.className = "sidebar-session-item__menu";
      menuTrigger.setAttribute("aria-label", t("session.actions"));
      menuTrigger.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h.01"/><path d="M12 12h.01"/><path d="M19 12h.01"/></svg>`;
      menuTrigger.addEventListener("click", (event) => {
        event.stopPropagation();
        if (activeSessionMenuId === session.id && sessionActionMenu && !sessionActionMenu.classList.contains("hidden")) {
          closeSessionMenu();
          return;
        }
        activeSessionMenuId = session.id;
        positionSessionMenu(menuTrigger);
      });
      row.appendChild(menuTrigger);
      return row;
    };

    if (normalizedFilter) {
      const matches = items
        .filter((turn) => turn.text.toLowerCase().includes(normalizedFilter))
        .slice()
        .reverse()
        .slice(0, 8);

      if (matches.length === 0) {
        const empty = document.createElement("li");
        empty.className = "sidebar-list__empty";
        empty.textContent = t("session.no_matches");
        elements.sessionList.appendChild(empty);
        if (elements.collapsedSessionsList) {
          const collapsedEmpty = document.createElement("li");
          collapsedEmpty.className = "sidebar-collapsed-sessions__empty";
          collapsedEmpty.textContent = t("session.no_matches");
          elements.collapsedSessionsList.appendChild(collapsedEmpty);
        }
        return;
      }

      matches.forEach((turn) => {
        const li = document.createElement("li");
        const session = sessions.find((entry) => entry.id === turn.id) || {
          id: turn.id,
          title: turn.text,
          pinned: false,
          current: false,
          turn,
          segment: [turn],
          segmentTurnIds: [turn.id],
        };
        li.appendChild(
          createSessionButton(session, () => {
            elements.commandInput.value = turn.role === "user" ? turn.text : elements.commandInput.value;
            autoResizeCommandInput();
            elements.commandInput.focus();
          })
        );
        elements.sessionList.appendChild(li);
      });
      return;
    }

    if (!sessions.length) {
      const empty = document.createElement("li");
      empty.className = "sidebar-list__empty";
      empty.textContent = t("session.no_items");
      elements.sessionList.appendChild(empty);
      if (elements.collapsedSessionsList) {
        const collapsedEmpty = document.createElement("li");
        collapsedEmpty.className = "sidebar-collapsed-sessions__empty";
        collapsedEmpty.textContent = t("session.no_items");
        elements.collapsedSessionsList.appendChild(collapsedEmpty);
      }
      return;
    }

    sessions.slice(0, 8).forEach((session) => {
        const li = document.createElement("li");
        li.appendChild(
          createSessionButton(session, () => {
              if (session.archived) {
                selectedArchivedSessionId = session.id;
                renderThread(session.segment || [], true);
                syncConversationState(session.segment || []);
              } else {
                selectedArchivedSessionId = "";
                renderThread(currentSnapshot?.conversation_turns || [], true);
                syncConversationState(items);
              }
              requestAnimationFrame(() => {
                elements.commandInput.focus();
              });
              refreshSessionFilter();
            })
        );
        elements.sessionList.appendChild(li);
      });

    if (elements.collapsedSessionsList) {
      sessions.slice(0, 8).forEach((session) => {
          const item = document.createElement("li");
          const button = document.createElement("button");
          button.type = "button";
          button.className = "sidebar-collapsed-sessions__item";
          button.textContent = truncate(session.title || t("session.current"), 42);
          button.addEventListener("click", () => {
            if (session.archived) {
              selectedArchivedSessionId = session.id;
              renderThread(session.segment || [], true);
              syncConversationState(session.segment || []);
            } else {
              selectedArchivedSessionId = "";
              renderThread(currentSnapshot?.conversation_turns || [], true);
              syncConversationState(items);
            }
            elements.commandInput.focus();
            elements.collapsedSessionsFlyout?.classList.add("hidden");
            elements.collapsedSessionsButton?.setAttribute("aria-expanded", "false");
            refreshSessionFilter();
          });
          item.appendChild(button);
          elements.collapsedSessionsList.appendChild(item);
        });
    }
  }

  function getConversationSearchItems(filterText = "") {
    const turns = getVisibleTurns(currentSnapshot?.conversation_turns || []);
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
    // L-16: CSS-escape turnId to prevent SyntaxError on special characters.
    const target = elements.threadList.querySelector(`[data-turn-id="${CSS.escape(turnId)}"]`);
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
    if (!elements.knowledgeBackend || !elements.knowledgeState || !elements.knowledgeResults) {
      return;
    }
    const retrieval = snapshot.retrieval_status || {};
    elements.knowledgeBackend.textContent = humanizeValue(retrieval.backend || "lexical");
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
      li.textContent = uiText(
        `${humanizeValue(hit.source_type, "Dokument")} · ${hit.metadata?.title || hit.source_id} · Relevanz ${Number(hit.score || 0).toFixed(2)} · ${truncate(hit.text || "", 88)}`,
        `${humanizeValue(hit.source_type, "Document")} · ${hit.metadata?.title || hit.source_id} · Relevance ${Number(hit.score || 0).toFixed(2)} · ${truncate(hit.text || "", 88)}`
      );
      elements.knowledgeResults.appendChild(li);
    });
  }

  function auditLang(deText, enText) {
    return (document.documentElement.lang || "en").toLowerCase().startsWith("de") ? deText : enText;
  }

  function auditIconMarkup(kind) {
    const icons = {
      disconnected: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.5 8.5 15.5 15.5M15.5 8.5 8.5 15.5"/><path d="M4 12c1.9-2.4 4.63-4 8-4 1.8 0 3.43.46 4.87 1.28M20 12c-.55.69-1.16 1.31-1.83 1.83M12 17a4.76 4.76 0 0 1-2.68-.81"/></svg>',
      connected: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12c1.9-2.4 4.63-4 8-4s6.1 1.6 8 4"/><path d="M7 15c1.2-1.38 2.98-2.25 5-2.25S15.8 13.62 17 15"/><path d="m9.5 18 1.5 1.5 3.5-3.5"/></svg>',
      upload: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V5"/><path d="m8.5 8.5 3.5-3.5 3.5 3.5"/><path d="M20 16.5v.5c0 2.21-1.79 4-4 4H8c-2.21 0-4-1.79-4-4v-.5"/></svg>',
      indexed: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5.5h9a2.5 2.5 0 0 1 2.5 2.5v10A2.5 2.5 0 0 1 14 20.5H7A2.5 2.5 0 0 1 4.5 18V6A.5.5 0 0 1 5 5.5Z"/><path d="m9 12 2 2 4-4"/></svg>',
      warning: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8v5"/><path d="M12 16.5h.01"/><path d="M10.3 4.9 3.8 16.3A2 2 0 0 0 5.54 19.3h12.92a2 2 0 0 0 1.74-3L13.7 4.9a2 2 0 0 0-3.4 0Z"/></svg>',
      activity: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h4l2-5 4 10 2-5h4"/><path d="M4 5.5h16"/></svg>',
    };
    return icons[kind] || icons.activity;
  }

  function humanizeAuditEvent(event) {
    const category = String(event.category || "").toLowerCase();
    const action = String(event.action || "");
    const message = String(event.message || "").trim();
    const combined = `${category} ${action} ${message}`.toLowerCase();
    const cleanMessage = message.replace(/\[(runtime|dashboard|documents|audit|network)\]\s*/gi, "").trim();

    if (combined.includes("websocket disconnected")) {
      return {
        tone: "warning",
        icon: "disconnected",
        label: auditLang("Verbindung", "Connection"),
        title: auditLang("Live-Verbindung getrennt", "Live connection lost"),
        detail: auditLang(
          "Die direkte Verbindung zum Dashboard wurde unterbrochen. KERN versucht automatisch, die Verbindung wiederherzustellen.",
          "The live dashboard link dropped for a moment. KERN will try to reconnect automatically."
        ),
      };
    }

    if (combined.includes("websocket connected")) {
      return {
        tone: "success",
        icon: "connected",
        label: auditLang("Verbindung", "Connection"),
        title: auditLang("Live-Verbindung wiederhergestellt", "Live connection restored"),
        detail: auditLang(
          "Das Dashboard ist wieder direkt mit KERN verbunden.",
          "The dashboard is directly connected to KERN again."
        ),
      };
    }

    if (combined.includes("processed") && combined.includes("uploaded file")) {
      const processed = message.match(/processed\s+(\d+)/i)?.[1];
      const indexed = message.match(/indexed\s+(\d+)/i)?.[1];
      const processedCount = Number(processed || 0);
      const indexedCount = Number(indexed || 0);
      return indexedCount > 0
        ? {
            tone: "success",
            icon: "upload",
            label: auditLang("Upload", "Upload"),
            title: auditLang("Dokumente hinzugefügt", "Documents added"),
            detail: auditLang(
              `${indexedCount} von ${processedCount} hochgeladenen Dateien sind jetzt durchsuchbar.`,
              `${indexedCount} of ${processedCount} uploaded files are now ready to search.`
            ),
          }
        : {
            tone: "warning",
            icon: "warning",
            label: auditLang("Upload", "Upload"),
            title: auditLang("Dateien geprüft", "Files checked"),
            detail: auditLang(
              `${processedCount} Dateien wurden geprüft. Diesmal wurde nichts Neues in die Suche aufgenommen.`,
              `${processedCount} files were checked. Nothing new was added to search this time.`
            ),
          };
    }

    if (combined.includes("ingest") && combined.includes("completed")) {
      const titleMatch = cleanMessage.match(/ingest\s+(.+?)(?:\s*\/|\.\s*$|$)/i);
      const docTitle = titleMatch?.[1]?.trim();
      return {
        tone: "success",
        icon: "indexed",
        label: auditLang("Dokument", "Document"),
        title: auditLang("Dokument bereit", "Document ready"),
        detail: docTitle
          ? auditLang(`${docTitle} ist jetzt lokal durchsuchbar.`, `${docTitle} is now searchable locally.`)
          : auditLang("Ein neues Dokument ist jetzt lokal durchsuchbar.", "A new document is now searchable locally."),
      };
    }

    const labelMap = {
      runtime: auditLang("System", "System"),
      documents: auditLang("Dokumente", "Documents"),
      network: auditLang("Netzwerk", "Network"),
      audit: auditLang("Protokoll", "Audit"),
    };
    return {
      tone: event.status || "neutral",
      icon: category === "network" ? "connected" : "activity",
      label: labelMap[category] || auditLang("Aktivität", "Activity"),
      title: action ? action.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase()) : auditLang("Aktualisierung", "Update"),
      detail: cleanMessage || auditLang("Es liegt eine neue lokale Statusmeldung vor.", "A new local status update is available."),
    };
  }

  function renderCapabilities(capabilities) {
    if (!elements.capabilityList) {
      return;
    }
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
    const activeModelName = info.llm_model || info.cognition_name || t("settings.ai_default");
    if (elements.settingsVersion) {
      elements.settingsVersion.textContent = `v${info.app_version || "0.0.0"}`;
    }
    if (elements.settingsModelName) {
      elements.settingsModelName.textContent = activeModelName;
    }
    if (elements.settingsModelType) {
      elements.settingsModelType.textContent = humanizeValue(info.cognition_type || t("settings.hybrid_stack"));
    }
    if (elements.settingsModelBackend) {
      elements.settingsModelBackend.textContent = humanizeValue(info.cognition_backend || snapshot.cognition_backend || "hybrid");
    }
    if (elements.settingsModelMode) {
      elements.settingsModelMode.textContent = humanizeValue(info.model_mode || "off");
    }
    if (elements.settingsModelPath) {
      elements.settingsModelPath.textContent = shortSettingPath(info.cognition_model_path, t("settings.not_configured"));
    }
    if (elements.settingsTextRuntime) {
      elements.settingsTextRuntime.textContent = formatSettingValue(info.text_runtime_url);
    }
    if (elements.settingsVisionRuntime) {
      const visionValue = info.vision_runtime_enabled ? formatSettingValue(info.vision_runtime_url) : t("settings.not_configured");
      elements.settingsVisionRuntime.textContent = visionValue;
    }
    if (elements.settingsFastModelPath) {
      elements.settingsFastModelPath.textContent = shortSettingPath(info.fast_model_path, t("settings.not_configured"));
    }
    if (elements.settingsDeepModelPath) {
      elements.settingsDeepModelPath.textContent = shortSettingPath(info.deep_model_path, t("settings.not_configured"));
    }
    if (elements.settingsEmbedModel) {
      elements.settingsEmbedModel.textContent = formatSettingValue(info.embed_model);
    }
    if (elements.settingsRetrievalBackend) {
      elements.settingsRetrievalBackend.textContent = humanizeValue(snapshot.retrieval_status?.backend || "lexical");
    }
    if (elements.settingsRetrievalHealth) {
      elements.settingsRetrievalHealth.textContent = humanizeValue(snapshot.retrieval_status?.index_health || "disabled");
    }
    if (elements.settingsOcrMode) {
      elements.settingsOcrMode.textContent = humanizeValue(info.ocr_mode || "native_then_ocr");
    }
    if (elements.settingsCloudMode) {
      elements.settingsCloudMode.textContent = info.cloud_available ? t("settings.cloud_available") : t("settings.local_only");
    }
    if (elements.settingsModelStory) {
      const llmReady = snapshot.llm_available === true;
      const cloudAllowed = Boolean(info.cloud_available);
      const isolated = snapshot.network_status?.status === "isolated";
      const retrievalState = snapshot.retrieval_status?.index_health || "disabled";
      const degradedReasons = snapshot.runtime_degraded_reasons || [];
      const tone = llmReady && !degradedReasons.length && !["stale", "missing"].includes(retrievalState) ? "success" : "warning";
      const facts = [
        t("settings.model_fact_runtime", { runtime: info.preferred_runtime || t("settings.ai_default") }),
        t("settings.model_fact_model", { model: activeModelName }),
        cloudAllowed ? t("settings.model_fact_cloud_enabled") : t("settings.model_fact_cloud_disabled"),
        isolated ? t("settings.model_fact_isolated") : t("settings.model_fact_local_endpoint"),
        uiText(`Text-Dienst: ${info.text_runtime_url || t("settings.not_configured")}`, `Text runtime: ${info.text_runtime_url || t("settings.not_configured")}`),
        uiText(
          `Bild-Dienst: ${info.vision_runtime_enabled ? (info.vision_runtime_url || t("settings.not_configured")) : "deaktiviert"}`,
          `Vision runtime: ${info.vision_runtime_enabled ? (info.vision_runtime_url || t("settings.not_configured")) : "disabled"}`
        ),
        uiText(`Suchindex: ${humanizeValue(retrievalState)}`, `Retrieval index: ${humanizeValue(retrievalState)}`),
        uiText(`Texterkennung: ${humanizeValue(info.ocr_mode || "native_then_ocr")}`, `OCR mode: ${humanizeValue(info.ocr_mode || "native_then_ocr")}`),
        t("settings.model_fact_fail_closed"),
      ];
      if (degradedReasons.length) {
        facts.push(uiText(`Hinweis: ${truncate(degradedReasons[0], 72)}`, `Attention: ${truncate(degradedReasons[0], 72)}`));
      }
      elements.settingsModelStory.dataset.tone = tone;
      elements.settingsModelStoryPill.textContent = llmReady && !degradedReasons.length ? t("settings.model_pill_ready") : t("settings.model_pill_attention");
      elements.settingsModelStoryTitle.textContent = llmReady && !degradedReasons.length ? t("settings.model_story_ready_title") : t("settings.model_story_warning_title");
      elements.settingsModelStoryText.textContent = llmReady
        ? degradedReasons.length
          ? degradedReasons.join(" ")
          : info.preferred_runtime_detail || t("settings.model_story_ready_body")
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

  function renderBackupStatus(snapshot) {
    if (!elements.settingsBackupStatusCard) {
      return;
    }
    const status = snapshot.backup_status || {};
    const locked = snapshot.profile_session?.unlocked === false;
    const totalBackups = Number(status.total_backups || 0);
    const lastBackupAt = status.latest_backup_at ? formatDateTime(status.latest_backup_at) : "";
    const restoreAttempt = status.last_restore_attempt_at ? formatDateTime(status.last_restore_attempt_at) : "";
    let tone = "empty";
    let pill = uiText("Offen", "Pending");
    let title = uiText("Für diesen Arbeitsbereich gibt es noch keine verschlüsselte Sicherung.", "No encrypted backup has been saved for this workspace yet.");
    let body = uiText("Erstellen Sie die erste Sicherung, damit eine Wiederherstellung später möglich ist.", "Create the first backup so this workspace can be restored later.");

    if (locked) {
      tone = "warning";
      pill = uiText("Gesperrt", "Locked");
      title = uiText("Entsperren Sie das aktive Profil, um Sicherungen und Wiederherstellungen anzusehen.", "Unlock the active profile to review backups and restore attempts.");
      body = uiText("Solange das Profil gesperrt ist, blendet KERN persönliche Wiederherstellungsdetails aus.", "While the profile is locked, KERN hides personal recovery details.");
    } else if (status.latest_backup_result === "failed") {
      tone = "warning";
      pill = uiText("Achtung", "Attention");
      title = uiText("Die letzte verschlüsselte Sicherung konnte nicht abgeschlossen werden.", "The last encrypted backup could not be completed.");
      body = status.latest_backup_error || uiText("Prüfen Sie den Speicherort und starten Sie die Sicherung erneut.", "Check the save location and try the backup again.");
    } else if (totalBackups > 0) {
      tone = status.last_restore_result === "failed" ? "warning" : "success";
      pill = status.last_restore_result === "failed" ? uiText("Wiederherstellung prüfen", "Restore issue") : uiText("Bereit", "Ready");
      title = uiText(
        `${totalBackups} verschlüsselte Sicherung${totalBackups === 1 ? "" : "en"} sind für diesen Arbeitsbereich verfügbar.`,
        `${totalBackups} encrypted backup${totalBackups === 1 ? "" : "s"} are available for this workspace.`
      );
      body = lastBackupAt
        ? uiText(`Die letzte Sicherung wurde am ${lastBackupAt} gespeichert.`, `The most recent backup was saved on ${lastBackupAt}.`)
        : uiText("Für diesen Arbeitsbereich liegen verschlüsselte Sicherungen vor.", "Encrypted backups are available for this workspace.");
    }

    elements.settingsBackupStatusCard.dataset.tone = tone;
    elements.settingsBackupStatusPill.textContent = pill;
    elements.settingsBackupStatusTitle.textContent = title;
    elements.settingsBackupStatusBody.textContent = body;
    elements.settingsBackupStatusFacts.innerHTML = "";
    const facts = [];
    if (status.latest_backup_label) {
      facts.push(uiText(`Speicherziel: ${status.latest_backup_label}`, `Backup destination: ${status.latest_backup_label}`));
    }
    if (status.latest_backup_path && !locked) {
      facts.push(uiText(`Letzte Datei: ${shortSettingPath(status.latest_backup_path)}`, `Latest file: ${shortSettingPath(status.latest_backup_path)}`));
    }
    if (restoreAttempt) {
      facts.push(uiText(`Letzter Wiederherstellungsversuch: ${restoreAttempt}`, `Last restore attempt: ${restoreAttempt}`));
    }
    if (status.last_restore_target && !locked) {
      facts.push(uiText(`Wiederherstellungsziel: ${shortSettingPath(status.last_restore_target)}`, `Restore target: ${shortSettingPath(status.last_restore_target)}`));
    }
    if (status.last_restore_result === "failed" && status.last_restore_error) {
      facts.push(uiText(`Wiederherstellungshinweis: ${truncate(status.last_restore_error, 76)}`, `Restore note: ${truncate(status.last_restore_error, 76)}`));
    } else if (status.last_restore_result === "success") {
      facts.push(uiText("Der letzte Wiederherstellungsversuch war erfolgreich.", "The last restore attempt finished successfully."));
    }
    if (!facts.length) {
      facts.push(
        uiText(
          `Sicherungsordner: ${shortSettingPath(snapshot.storage_roots?.backups || snapshot.active_profile?.backups_root, t("settings.not_configured"))}`,
          `Backup folder: ${shortSettingPath(snapshot.storage_roots?.backups || snapshot.active_profile?.backups_root, t("settings.not_configured"))}`
        )
      );
    }
    facts.forEach((fact) => {
      const li = document.createElement("li");
      li.textContent = fact;
      elements.settingsBackupStatusFacts.appendChild(li);
    });
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
        window.dispatchEvent(
          new CustomEvent("kern:composer-doc-context", {
            detail: { documentIds: doc.id ? [doc.id] : [] },
          }),
        );
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
      const view = humanizeAuditEvent(event);
      li.className = `detail-list__item audit-entry audit-entry--${view.tone || "neutral"}`;
      li.innerHTML = `
        <span class="audit-entry__icon" aria-hidden="true">${auditIconMarkup(view.icon)}</span>
        <div class="audit-entry__copy">
          <span class="audit-entry__eyebrow">${escapeHTML(view.label || "")}</span>
          <strong>${escapeHTML(view.title || "")}</strong>
          <small>${escapeHTML(view.detail || "")}</small>
        </div>
        <span class="audit-status audit-status--${escapeHTML(event.status || "neutral")}">${escapeHTML(event.status || auditLang("info", "info"))}</span>
      `;
      list.appendChild(li);
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
      title.textContent = alert.title || t("alerts.default");
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
        actionBtn.textContent = t("alerts.take_action");
        actionBtn.addEventListener("click", () => {
          send({ type: "submit_text", text: alert.message });
        });
        actions.appendChild(actionBtn);
      }
      const dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "ghost-button";
      dismissBtn.dataset.testid = "proactive-alert-dismiss";
      dismissBtn.textContent = t("alerts.dismiss");
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
    elements.settingsMemoryScope.textContent = humanizeValue(snapshot.memory_scope || "profile");
    if (elements.settingsChatStyleMode && document.activeElement !== elements.settingsChatStyleMode) {
      elements.settingsChatStyleMode.value = snapshot.chat_style_mode || "natural";
    }
    if (elements.settingsChatCustomPrompt) {
      const nextPrompt = snapshot.chat_custom_prompt || "";
      if (elements.settingsChatCustomPrompt.dataset.dirty === "true") {
        if (elements.settingsChatCustomPrompt.value === nextPrompt) {
          elements.settingsChatCustomPrompt.dataset.dirty = "false";
        }
      } else if (elements.settingsChatCustomPrompt.value !== nextPrompt) {
        elements.settingsChatCustomPrompt.value = nextPrompt;
      }
    }
    elements.settingsDocumentsRoot.textContent = shortSettingPath(snapshot.storage_roots?.documents, t("settings.not_configured"));
    elements.settingsArchiveRoot.textContent = shortSettingPath(snapshot.storage_roots?.archives, t("settings.not_configured"));
    elements.settingsBackupRoot.textContent = shortSettingPath(snapshot.storage_roots?.backups, t("settings.not_configured"));
    if (elements.settingsReadinessStatus) {
      const readiness = snapshot.readiness_summary || {};
      const counts = [];
      if (readiness.errors) {
        counts.push(uiText(`${readiness.errors} Fehler`, `${readiness.errors} error${readiness.errors === 1 ? "" : "s"}`));
      }
      if (readiness.warnings) {
        counts.push(uiText(`${readiness.warnings} Hinweis${readiness.warnings === 1 ? "" : "e"}`, `${readiness.warnings} warning${readiness.warnings === 1 ? "" : "s"}`));
      }
      elements.settingsReadinessStatus.textContent = readiness.headline
        ? counts.length
          ? `${readiness.headline} (${counts.join(", ")})`
          : readiness.headline
        : t("settings.readiness_checking");
    }
    elements.settingsAuditState.textContent = snapshot.audit_enabled
      ? snapshot.recent_audit_events?.length
        ? t("audit.enabled_live")
        : t("audit.enabled")
      : t("audit.disabled");
    elements.settingsAuditChain.textContent = snapshot.audit_chain_ok
      ? t("audit.verified")
      : `${t("sync.degraded")}${snapshot.audit_chain_reason ? ` / ${truncate(snapshot.audit_chain_reason, 52)}` : ""}`;
    elements.settingsDbEncryption.textContent = snapshot.security_status?.db_encryption_enabled
      ? uiText(
          `${humanizeValue(snapshot.security_status.db_encryption_mode || "encrypted")} aktiv`,
          `${humanizeValue(snapshot.security_status.db_encryption_mode || "encrypted")} active`
        )
      : t("misc.off");
    elements.settingsKeyVersion.textContent = String(snapshot.security_status?.key_version || 0);
    elements.settingsArtifactEncryption.textContent = snapshot.security_status?.artifact_encryption_enabled
      ? uiText(
          `${humanizeValue(snapshot.security_status.artifact_encryption_status || t("overview.encrypted"))}${snapshot.security_status?.artifact_encryption_migration_state ? ` · ${humanizeValue(snapshot.security_status.artifact_encryption_migration_state)}` : ""}`,
          `${humanizeValue(snapshot.security_status.artifact_encryption_status || t("overview.encrypted"))}${snapshot.security_status?.artifact_encryption_migration_state ? ` · ${humanizeValue(snapshot.security_status.artifact_encryption_migration_state)}` : ""}`
        )
      : t("misc.disabled");
    elements.settingsSupportBundlePath.textContent = snapshot.support_bundle_path
      ? shortSettingPath(snapshot.support_bundle_path)
      : t("settings.support_bundle_none");
    elements.settingsSupportBundleLastExport.textContent = snapshot.support_bundle_last_export_at
      ? formatDateTime(snapshot.support_bundle_last_export_at)
      : t("settings.not_exported");
    elements.settingsUpdateChannel.textContent = humanizeValue(snapshot.update_channel || "stable");
    renderBackupStatus(snapshot);
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
        `${t("settings.update_last_backup")} ${updateState.last_backup_at ? formatDateTime(updateState.last_backup_at) : t("settings.not_exported")}`,
        `${t("settings.update_last_restore")} ${updateState.last_restore_attempt_at ? formatDateTime(updateState.last_restore_attempt_at) : t("settings.never")}`,
      ];
      if (updateState.last_error) {
        facts.push(`${t("settings.update_last_error")} ${truncate(updateState.last_error, 80)}`);
      }
      elements.settingsUpdateCard.dataset.tone = tone;
      elements.settingsUpdatePill.textContent = updatePillMap[updateState.last_status || "idle"] || titleCase(updateState.last_status || "idle");
      elements.settingsUpdateTitle.textContent = uiText(
        updateState.policy === "Manual stable-channel updates only."
          ? "Es werden nur manuelle Stable-Updates verwendet."
          : updateState.policy || t("settings.update_default_title"),
        updateState.policy || t("settings.update_default_title")
      );
      elements.settingsUpdateBody.textContent = uiText(
        updateState.message === "Manual stable-channel updates only."
          ? "Updates laufen nur im Stable-Kanal und werden hier bewusst manuell angestoßen."
          : updateState.message || t("settings.update_default_body"),
        updateState.message || t("settings.update_default_body")
      );
      elements.settingsUpdateFacts.innerHTML = "";
      facts.forEach((fact) => {
        const li = document.createElement("li");
        li.textContent = fact;
        elements.settingsUpdateFacts.appendChild(li);
      });
    }
    renderOverview(snapshot);
    const totals = snapshot.domain_totals || {};
    setOptionalText(elements.settingsDocumentCount, String(totals.documents || 0));
    setOptionalText(elements.settingsBusinessCount, String(totals.business_documents || 0));
    setOptionalText(elements.settingsSyncCount, String(totals.sync_targets || 0));
    setOptionalText(elements.systemProfileName, profile.slug || "default");
    setOptionalText(elements.systemProfileState, stateLabel);
    setOptionalText(elements.systemMemoryScope, humanizeValue(snapshot.memory_scope || "profile"));
    setOptionalDisabled(elements.settingsCreateBackup, locked);
    setOptionalDisabled(elements.settingsLockProfile, locked);
    setOptionalDisabled(elements.settingsUnlockProfile, !locked);
    setOptionalDisabled(elements.utilityToggle, locked);
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
    renderBusinessDocuments(snapshot.business_documents || []);
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
  }

  function renderProfileLock(snapshot) {
    if (!elements.profileLockPanel) {
      return;
    }
    const session = snapshot?.profile_session || {};
    const locked = session.unlocked === false;
    elements.profileLockPanel.classList.toggle("hidden", !locked);
    if (elements.conversationShell) {
      elements.conversationShell.classList.toggle("hidden", locked);
    }
    if (elements.failurePanel) {
      elements.failurePanel.classList.toggle("hidden", locked || !(snapshot?.active_failures || []).length);
    }
    if (!locked) {
      return;
    }
    const workspaceTitle = snapshot?.active_profile?.title || snapshot?.active_profile?.slug || "workspace";
    if (elements.profileLockTitle) {
      elements.profileLockTitle.textContent = uiText(
        `${workspaceTitle} entsperren, um weiterzumachen.`,
        `Unlock ${workspaceTitle} to continue.`
      );
    }
    if (elements.profileLockBody) {
      elements.profileLockBody.textContent = session.locked_reason
        ? uiText(
            `${session.locked_reason} Geben Sie die Profil-PIN ein, um Dokumente, Gespräche und Einstellungen wiederherzustellen.`,
            `${session.locked_reason} Enter the profile PIN to restore documents, conversations, and settings.`
          )
        : uiText(
            "Geben Sie die Profil-PIN ein, um Dokumente, Gespräche und Einstellungen wiederherzustellen.",
            "Enter the profile PIN to restore documents, conversations, and settings."
          );
    }
    if (elements.profileLockMessage) {
      elements.profileLockMessage.textContent = t("actions.unlock_required");
    }
  }

  function renderRuntimeSlice(snapshot) {
    ensureSidebarShellControls();
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

    if (elements.localModeToggle) {
      elements.localModeToggle.checked = Boolean(snapshot.local_mode_enabled);
    }
    if (elements.deviceProfileMeta) {
      elements.deviceProfileMeta.textContent = snapshot.verification_state || t("misc.no_verified_actions");
    }
    renderTrustBadge(snapshot.network_status || {});
    renderAuditLog(snapshot.recent_audit_events || [], snapshot.network_status || {});
    renderProactiveAlerts(snapshot.proactive_alerts || []);
    renderMemoryTimeline(snapshot.memory_timeline || []);
    renderPlatformInfo(snapshot);
    renderKnowledge(snapshot);
    renderProfileLock(snapshot);
    renderOnboarding(snapshot);
    renderSidebarWorkspaces(snapshot);
    renderSidebarSystemStatus(snapshot);
    renderFailures(snapshot);
    setActionLock(Boolean(snapshot.action_in_progress) || snapshot.profile_session?.unlocked === false);
    renderList(elements.activityLog, snapshot.action_history || [], (item) => `[${item.category}] ${item.message}`);
    renderPlan(snapshot.active_plan);
    renderModelInfo(snapshot);
    renderThemeState();
    setOptionalText(elements.checkMemory, snapshot.startup_checks?.memory || t("misc.unknown"));
    setOptionalText(elements.checkCognition, snapshot.startup_checks?.cognition || snapshot.cognition_backend || t("misc.unknown"));
  }

  function renderConversationSlice(snapshot) {
    const shouldAutoFollow = isThreadNearBottom();
    const archivedSession = selectedArchivedSessionId
      ? getArchivedSessions().find((session) => session.id === selectedArchivedSessionId)
      : null;
    renderThread(archivedSession?.turns || snapshot.conversation_turns || [], shouldAutoFollow);
    renderSidebarSessionList(snapshot.conversation_turns || [], "");
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
    setOptionalText(elements.proactiveReason, snapshot.proactive_prompt?.reason || t("proactive.no_prompt"));
    setOptionalText(elements.proactiveText, snapshot.proactive_prompt?.message || t("proactive.quiet"));

    if (snapshot.morning_brief?.focus_suggestion) {
      setOptionalText(elements.focusText, snapshot.morning_brief.focus_suggestion);
      return;
    }
    setOptionalText(elements.focusText, t("brief.default_focus"));
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

  function queueSnapshotRender(snapshot) {
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
      renderSidebarSessionList(currentSnapshot.conversation_turns || [], "");
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

  /**
   * SECURITY: All innerHTML assignments in this module MUST escape dynamic
   * content via escapeHTML/escapeHtml before interpolation.  Use this helper
   * for tagged template literals to make the safe path the default:
   *   el.innerHTML = safeHTML`<span>${unsafeValue}</span>`;
   */
  function safeHTML(strings, ...values) {
    return strings.reduce((result, str, i) => {
      const val = i < values.length ? escapeHtml(String(values[i] ?? "")) : "";
      return result + str + val;
    }, "");
  }

  function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // C-09: DOM-based highlighting to avoid innerHTML XSS.
  function highlightTermsDOM(container, text, terms) {
    container.textContent = "";
    if (!terms.length) {
      container.textContent = text;
      return;
    }
    const pattern = new RegExp(`(${terms.map(escapeRegex).join("|")})`, "gi");
    let lastIndex = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > lastIndex) {
        container.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
      }
      const mark = document.createElement("mark");
      mark.className = "passage-highlight";
      mark.textContent = match[1];
      container.appendChild(mark);
      lastIndex = pattern.lastIndex;
    }
    if (lastIndex < text.length) {
      container.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
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
      highlightTermsDOM(elements.passageBody, text, terms);
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
    queueSnapshotRender,
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
    archiveCurrentConversation() {
      const archived = archiveTurns(currentSnapshot?.conversation_turns || []);
      refreshSessionFilter();
      return archived;
    },
    clearArchivedSelection() {
      if (!selectedArchivedSessionId) {
        return;
      }
      selectedArchivedSessionId = "";
      if (currentSnapshot) {
        renderThread(currentSnapshot.conversation_turns || [], true);
        refreshSessionFilter();
      }
    },
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
