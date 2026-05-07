import { escapeHTML, secureFetch } from "/static/js/utils.js?v=20260419k";

function formatDate(value) {
  if (!value) return "Unknown time";
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(value));
  } catch {
    return String(value);
  }
}

async function fetchJson(url, options = {}) {
  const response = await secureFetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || payload.message || "Request failed.");
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function renderMetricGrid(element, cards = []) {
  if (!element) return;
  if (!cards.length) {
    element.innerHTML = "";
    return;
  }
  element.innerHTML = cards
    .map(
      (card) => `
        <article class="workbench-metric-card">
          <span>${escapeHTML(card.label || "")}</span>
          <strong>${escapeHTML(card.value || "0")}</strong>
          <small>${escapeHTML(card.detail || "")}</small>
        </article>
      `
    )
    .join("");
}

function isGerman() {
  return (document.documentElement.lang || "en").toLowerCase().startsWith("de");
}

function wt(deText, enText) {
  return isGerman() ? deText : enText;
}

function translateWorkbenchPayloadText(value) {
  const text = String(value || "").trim();
  if (!text || !isGerman()) return text;

  const directMap = new Map([
    ["Prepared blocker brief for compliance work", "Vorbereitete Blocker-Zusammenfassung fuer die Compliance-Pruefung"],
    ["Legal hold and retention state are clear", "Sperrvermerk und Aufbewahrung sind geklaert"],
    ["Resolve blocker", "Blocker aufloesen"],
    ["Manual approval is active.", "Manuelle Freigabe ist aktiv."],
    ["Workspace creation, user approval, and session revocation are role-gated and auditable.", "Arbeitsbereiche, Freigaben und Sitzungsentzug sind hier rollenbasiert und nachvollziehbar."],
    ["Prepared work is ready to review.", "Vorbereitete Aufgabe ist bereit zur Pruefung."],
    ["No summary yet.", "Noch keine Zusammenfassung vorhanden."],
    ["No additional information is needed right now.", "Aktuell werden keine weiteren Angaben benoetigt."],
    ["No event", "Kein Ereignis"],
    ["none", "keins"]
  ]);

  if (directMap.has(text)) {
    return directMap.get(text);
  }

  return text
    .replace(/A legal hold or retention rule is blocking at least one requested compliance step\./gi, "Ein Sperrvermerk oder eine Aufbewahrungsregel blockiert derzeit mindestens einen angefragten Compliance-Schritt.")
    .replace(/Conflicting evidence must be resolved by a worker before this packet is trusted\./gi, "Widerspruechliche Nachweise muessen erst geklaert werden, bevor dieses Paket vertraut werden kann.")
    .replace(/Prepared blocker brief for compliance work/gi, "Vorbereitete Blocker-Zusammenfassung fuer die Compliance-Pruefung")
    .replace(/Legal hold and retention state are clear/gi, "Sperrvermerk und Aufbewahrung sind geklaert")
    .replace(/Resolve blocker/gi, "Blocker aufloesen")
    .replace(/\bnext\b/gi, "naechster Schritt")
    .replace(/recorded event\(s\)/gi, "erfasste Ereignisse")
    .replace(/\bblocking\b/gi, "blockiert")
    .replace(/\bblocked\b/gi, "blockiert")
    .replace(/\bwaiting on input\b/gi, "wartet auf Angaben")
    .replace(/\bready now\b/gi, "sofort bereit");
}

function humanizeToken(value, fallback = null) {
  if (value == null || value === "") return fallback || wt("Nicht verfügbar", "Not available");
  return String(value)
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function shortPath(value) {
  if (!value) return wt("Nicht eingerichtet", "Not set up");
  const parts = String(value).split(/[\\/]+/).filter(Boolean);
  if (parts.length <= 2) return String(value);
  return `.../${parts.slice(-2).join("/")}`;
}

function prettyRole(role) {
  const map = {
    org_owner: wt("Inhaber", "Owner"),
    org_admin: wt("Administrator", "Administrator"),
    auditor: wt("Prüfer", "Auditor"),
    member: wt("Mitglied", "Member"),
    break_glass_admin: wt("Notfallzugriff", "Emergency access"),
  };
  return map[role] || humanizeToken(role);
}

function prettyRoles(roles = []) {
  return roles.length ? roles.map(prettyRole).join(", ") : wt("Keine Rolle vergeben", "No role assigned");
}

function prettyAuthSource(value) {
  const map = {
    password: wt("Passwort-Anmeldung", "Password sign-in"),
    session: wt("Aktive Sitzung", "Active session"),
    bootstrap: wt("Ersteinrichtung", "Bootstrap setup"),
    break_glass: wt("Notfallzugriff", "Emergency access"),
  };
  return map[value] || humanizeToken(value, wt("Lokale Anmeldung", "Local sign-in"));
}

function prettyStatus(value) {
  const map = {
    active: wt("Aktiv", "Active"),
    available: wt("Verfügbar", "Available"),
    pending: wt("Wartet", "Pending"),
    approved: wt("Bestätigt", "Approved"),
    rejected: wt("Abgelehnt", "Rejected"),
    completed: wt("Abgeschlossen", "Completed"),
    requested: wt("Angefragt", "Requested"),
    blocked: wt("Blockiert", "Blocked"),
    released: wt("Freigegeben", "Released"),
    revoked: wt("Beendet", "Revoked"),
    success: wt("Erfolgreich", "Successful"),
    failed: wt("Fehlgeschlagen", "Failed"),
    ready_now: wt("Bereit", "Ready"),
    waiting_on_input: wt("Wartet auf Infos", "Waiting on info"),
    warning: wt("Hinweis", "Heads-up"),
    info: wt("Info", "Info"),
  };
  return map[value] || humanizeToken(value, wt("Info", "Info"));
}

function prettyScope(value) {
  const map = {
    workspace: wt("Arbeitsbereich", "Workspace"),
    profile: wt("Profil", "Profile"),
    profile_plus_archive: wt("Profil und Archiv", "Profile and archive"),
    personal_only: wt("Nur persönlich", "Personal only"),
  };
  return map[value] || humanizeToken(value, wt("Arbeitsbereich", "Workspace"));
}

function prettyDataClass(value) {
  const map = {
    regulated_business: wt("Regelgebundenes Dokument", "Regulated document"),
    personal: wt("Persönliche Daten", "Personal data"),
    finance: wt("Finanzdaten", "Finance data"),
    hr: wt("Personaldaten", "HR data"),
    unclassified: wt("Nicht klassifiziert", "Unclassified"),
  };
  return map[value] || humanizeToken(value, wt("Nicht klassifiziert", "Unclassified"));
}

function prettyDecision(value) {
  const map = {
    exportable: wt("Export möglich", "Can be exported"),
    erasable: wt("Kann gelöscht werden", "Can be erased"),
    not_exportable: wt("Nicht exportierbar", "Not exportable"),
    retention_bound: wt("Aufbewahrung gilt", "Retention applies"),
    pseudonymize_only: wt("Nur pseudonymisieren", "Pseudonymize only"),
  };
  return map[value] || humanizeToken(value);
}

function rowMeta(parts) {
  const filtered = parts
    .filter(Boolean)
    .map((part) => (typeof part === "string" ? translateWorkbenchPayloadText(part) : part));
  if (!filtered.length) return "";
  return `<div class="workbench-inline-meta">${filtered.map((part) => `<span>${escapeHTML(part)}</span>`).join("")}</div>`;
}

function actionButtons(actions = []) {
  if (!actions.length) return "";
  return `
    <div class="workbench-action-row">
      ${actions
        .map(
          (action) => `
            <button
              type="button"
              class="${action.primary ? "solid-button" : "ghost-button"}"
              data-workbench-action="${escapeHTML(action.type)}"
              ${Object.entries(action.dataset || {})
                .map(([key, value]) => `data-${key}="${escapeHTML(value)}"`)
                .join(" ")}
            >${escapeHTML(action.label)}</button>
          `
        )
        .join("")}
    </div>
  `;
}

function renderList(element, items, renderItem, emptyText) {
  if (!element) return;
  if (!items?.length) {
    element.innerHTML = `
      <li class="detail-list__item--state">
        <div class="panel-state">
          <div class="panel-state__head">
            <span class="panel-state__pill">${escapeHTML(wt("Leer", "Empty"))}</span>
            <strong class="panel-state__title">${escapeHTML(emptyText)}</strong>
          </div>
        </div>
      </li>
    `;
    return;
  }
  element.innerHTML = items.map(renderItem).join("");
}

function setStateCard(element, title, body, tone = "warning") {
  if (!element) return;
  element.classList.remove("panel-state--success", "panel-state--warning", "panel-state--error");
  element.classList.add(`panel-state--${tone}`);
  const titleEl = element.querySelector(".panel-state__title");
  const bodyEl = element.querySelector(".panel-state__body");
  if (titleEl) titleEl.textContent = translateWorkbenchPayloadText(title);
  if (bodyEl) bodyEl.textContent = translateWorkbenchPayloadText(body);
}

export function createWorkbenchController({ renderer }) {
  const state = {
    session: null,
    activeTab: "workspace",
  };

  const dom = {
    modal: document.getElementById("utilityModal"),
    tabs: [...document.querySelectorAll(".utility-tab")],
    workspaceSummaryState: document.getElementById("workspaceSummaryState"),
    workspaceSummaryTitle: document.getElementById("workspaceSummaryTitle"),
    workspaceSummaryBody: document.getElementById("workspaceSummaryBody"),
    workspaceMetricGrid: document.getElementById("workspaceMetricGrid"),
    workspaceAccessList: document.getElementById("workspaceAccessList"),
    adminAccessState: document.getElementById("adminAccessState"),
    adminMetricGrid: document.getElementById("adminMetricGrid"),
    adminWorkspaceList: document.getElementById("adminWorkspaceList"),
    adminPendingUsersList: document.getElementById("adminPendingUsersList"),
    adminUsersList: document.getElementById("adminUsersList"),
    adminSessionsList: document.getElementById("adminSessionsList"),
    adminWorkspaceSlug: document.getElementById("adminWorkspaceSlug"),
    adminWorkspaceTitle: document.getElementById("adminWorkspaceTitle"),
    adminUserEmail: document.getElementById("adminUserEmail"),
    adminUserDisplayName: document.getElementById("adminUserDisplayName"),
    adminUserRole: document.getElementById("adminUserRole"),
    adminUserWorkspace: document.getElementById("adminUserWorkspace"),
    complianceStateCard: document.getElementById("complianceStateCard"),
    complianceMetricGrid: document.getElementById("complianceMetricGrid"),
    complianceRetentionList: document.getElementById("complianceRetentionList"),
    complianceHoldList: document.getElementById("complianceHoldList"),
    complianceErasureList: document.getElementById("complianceErasureList"),
    complianceExportList: document.getElementById("complianceExportList"),
    complianceInventoryList: document.getElementById("complianceInventoryList"),
    retentionDataClass: document.getElementById("retentionDataClass"),
    retentionDays: document.getElementById("retentionDays"),
    retentionLegalHoldEnabled: document.getElementById("retentionLegalHoldEnabled"),
    legalHoldReason: document.getElementById("legalHoldReason"),
    legalHoldWorkspace: document.getElementById("legalHoldWorkspace"),
    selfErasureReason: document.getElementById("selfErasureReason"),
    regulatedCandidateList: document.getElementById("regulatedCandidateList"),
    regulatedDocumentWorkbenchList: document.getElementById("regulatedDocumentWorkbenchList"),
    intelligenceStateCard: document.getElementById("intelligenceStateCard"),
    intelligenceMetricGrid: document.getElementById("intelligenceMetricGrid"),
    recommendationList: document.getElementById("recommendationList"),
    obligationList: document.getElementById("obligationList"),
    workflowList: document.getElementById("workflowList"),
    decisionHistoryList: document.getElementById("decisionHistoryList"),
    promotionCandidatesList: document.getElementById("promotionCandidatesList"),
    trainingExamplesList: document.getElementById("trainingExamplesList"),
    trainingExportsList: document.getElementById("trainingExportsList"),
    evidenceStateCard: document.getElementById("evidenceStateCard"),
    evidenceMetricGrid: document.getElementById("evidenceMetricGrid"),
    evidenceManifestList: document.getElementById("evidenceManifestList"),
    evidenceTrainingManifestList: document.getElementById("evidenceTrainingManifestList"),
  };

  function currentRoles() {
    return state.session?.roles || [];
  }

  function hasRole(...roles) {
    if (state.session?.is_break_glass) return true;
    return roles.some((role) => currentRoles().includes(role));
  }

  function isMemberOnly() {
    return !!state.session && !hasRole("org_owner", "org_admin", "auditor");
  }

  function populateWorkspaceOptions(workspaces = []) {
    const selects = [dom.adminUserWorkspace, dom.legalHoldWorkspace].filter(Boolean);
    for (const select of selects) {
      const current = select.value;
      select.innerHTML = workspaces
        .map(
          (workspace) => `<option value="${escapeHTML(workspace.slug)}">${escapeHTML(workspace.title || workspace.slug)}</option>`
        )
        .join("");
      if (current && workspaces.some((workspace) => workspace.slug === current)) {
        select.value = current;
      } else if (state.session?.workspace_slug) {
        select.value = state.session.workspace_slug;
      }
    }
  }

  function applyRoleVisibility() {
    const roles = new Set(currentRoles());
    dom.tabs.forEach((tab) => {
      const allowed = String(tab.dataset.roleAllow || "")
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean);
      const visible = !allowed.length || state.session?.is_break_glass || allowed.some((role) => roles.has(role));
      tab.classList.toggle("is-role-hidden", !visible);
      tab.toggleAttribute("hidden", !visible);
    });
  }

  async function refreshSession() {
    state.session = await fetchJson("/auth/session");
    populateWorkspaceOptions(state.session.workspaces || []);
    applyRoleVisibility();
    if (dom.workspaceSummaryTitle) {
      dom.workspaceSummaryTitle.textContent = state.session.workspace_title || state.session.workspace_slug || wt("Kein Arbeitsbereich ausgewählt", "No workspace selected");
    }
    if (dom.workspaceSummaryBody) {
      const email = state.session.user?.email || state.session.user_email || wt("Anonym", "Anonymous");
      const workspaceName = state.session.workspace_title || state.session.workspace_slug || wt("keinem Arbeitsbereich", "no workspace");
      dom.workspaceSummaryBody.textContent = wt(
        `${email} arbeitet gerade in ${workspaceName} mit ${prettyRoles(currentRoles())}.`,
        `${email} is currently working in ${workspaceName} as ${prettyRoles(currentRoles())}.`
      );
    }
    return state.session;
  }

  async function renderWorkspace() {
    await refreshSession();
    const workspaces = state.session.workspaces || [];
    renderMetricGrid(dom.workspaceMetricGrid, [
      { label: wt("Arbeitsbereiche", "Workspaces"), value: String(workspaces.length), detail: wt("In dieser Sitzung verfügbar", "Available in this session") },
      { label: wt("Aktiv", "Selected"), value: state.session.workspace_title || state.session.workspace_slug || wt("Keiner", "None"), detail: prettyAuthSource(state.session.auth_method || "session") },
      { label: wt("Rollen", "Roles"), value: String(currentRoles().length || 0), detail: prettyRoles(currentRoles()) },
      { label: wt("Sitzungen", "Sessions"), value: String((state.session.sessions || []).length), detail: wt("Gerade angemeldet", "Currently signed in") },
    ]);
    renderList(
      dom.workspaceAccessList,
      workspaces,
      (workspace) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(workspace.title || workspace.slug)}</strong>
            <span class="workbench-pill" data-tone="${workspace.slug === state.session.workspace_slug ? "success" : "warning"}">${workspace.slug === state.session.workspace_slug ? prettyStatus("active") : prettyStatus("available")}</span>
          </div>
          ${rowMeta([`${wt("Kennung", "ID")}: ${workspace.slug}`, workspace.profile_root ? wt("Lokal gespeichert", "Stored locally") : null])}
        </li>
      `,
      wt("Diesem Konto ist noch kein Arbeitsbereich zugeordnet.", "No workspace is assigned to this account yet.")
    );
  }

  async function renderAdmin() {
    if (!hasRole("org_owner", "org_admin")) {
      setStateCard(dom.adminAccessState, wt("Verwaltung ist hier nicht freigeschaltet.", "Admin controls are not available here."), wt("Dieses Konto sieht nur die Bereiche, die im aktuellen Arbeitsbereich erlaubt sind.", "This account can only see the surfaces allowed in the current workspace."), "error");
      renderMetricGrid(dom.adminMetricGrid, []);
      [dom.adminWorkspaceList, dom.adminPendingUsersList, dom.adminUsersList, dom.adminSessionsList].forEach((element) =>
        renderList(element, [], () => "", wt("Für diese Rolle nicht freigegeben.", "Not available for this role."))
      );
      return;
    }
    const [workspacesPayload, usersPayload, sessionsPayload] = await Promise.all([
      fetchJson("/admin/workspaces"),
      fetchJson("/admin/users"),
      fetchJson("/admin/sessions"),
    ]);
    const workspaces = workspacesPayload.items || workspacesPayload.workspaces || [];
    const users = usersPayload.items || usersPayload.users || [];
    const sessions = sessionsPayload.items || sessionsPayload.sessions || [];
    const pendingUsers = users.filter((user) => String(user.status || "").toLowerCase() === "pending");
    populateWorkspaceOptions(workspaces);
    renderMetricGrid(dom.adminMetricGrid, [
      { label: wt("Arbeitsbereiche", "Workspaces"), value: String(workspaces.length), detail: wt("Für dieses Team angelegt", "Set up for this team") },
      { label: wt("Personen", "People"), value: String(users.length), detail: wt("Mit Zugriff auf das System", "With access to the system") },
      { label: wt("Wartend", "Pending"), value: String(pendingUsers.length), detail: wt("Warten auf Freigabe", "Waiting for approval") },
      { label: wt("Sitzungen", "Sessions"), value: String(sessions.length), detail: wt("Derzeit aktiv", "Currently active") },
    ]);
    renderList(
      dom.adminWorkspaceList,
      workspaces,
      (workspace) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(workspace.title || workspace.slug)}</strong>
            <span class="workbench-pill" data-tone="${workspace.slug === state.session.workspace_slug ? "success" : "warning"}">${workspace.slug === state.session.workspace_slug ? prettyStatus("active") : prettyStatus("available")}</span>
          </div>
          ${rowMeta([`${wt("Kennung", "ID")}: ${workspace.slug}`, workspace.backup_root ? wt("Sicherungen sind eingerichtet", "Backups are set up") : wt("Noch kein Sicherungsziel", "No backup destination yet")])}
        </li>
      `,
      wt("Es gibt noch keine angelegten Arbeitsbereiche.", "No workspaces have been created yet.")
    );
    renderList(
      dom.adminPendingUsersList,
      pendingUsers,
      (user) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(user.display_name || user.email || user.id)}</strong>
            <span class="workbench-pill" data-tone="warning">${escapeHTML(prettyStatus(user.status || "pending"))}</span>
          </div>
          ${rowMeta([user.email, prettyAuthSource(user.auth_source)])}
          ${actionButtons([
            { type: "approve-user", label: wt("Freigeben", "Approve"), primary: true, dataset: { userId: user.id } },
            { type: "suspend-user", label: wt("Sperren", "Suspend"), dataset: { userId: user.id } },
          ])}
        </li>
      `,
      wt("Zurzeit wartet niemand auf eine Freigabe.", "No one is currently waiting for approval.")
    );
    renderList(
      dom.adminUsersList,
      users,
      (user) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(user.display_name || user.email || user.id)}</strong>
            <span class="workbench-pill" data-tone="${String(user.status).toLowerCase() === "active" ? "success" : "warning"}">${escapeHTML(prettyStatus(user.status || "info"))}</span>
          </div>
          ${rowMeta([user.email, prettyAuthSource(user.auth_source)])}
          ${String(user.status).toLowerCase() === "active" ? actionButtons([{ type: "suspend-user", label: wt("Sperren", "Suspend"), dataset: { userId: user.id } }]) : ""}
        </li>
      `,
      wt("Noch keine Personen vorhanden.", "No people are available yet.")
    );
    renderList(
      dom.adminSessionsList,
      sessions,
      (session) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(session.user_id || wt("Dienstsitzung", "Service session"))}</strong>
            <span class="workbench-pill" data-tone="${session.revoked_at ? "danger" : "success"}">${session.revoked_at ? prettyStatus("revoked") : prettyStatus("active")}</span>
          </div>
          ${rowMeta([session.workspace_slug, formatDate(session.last_activity_at), prettyAuthSource(session.auth_method)])}
          ${session.revoked_at ? "" : actionButtons([{ type: "revoke-session", label: wt("Beenden", "End session"), dataset: { sessionId: session.id } }])}
        </li>
      `,
      wt("Es wurden keine aktiven Sitzungen gefunden.", "No active sessions were found.")
    );
  }

  async function renderCompliance() {
    await refreshSession();
    const selfServiceCards = [
      { label: wt("Bereich", "Scope"), value: state.session.workspace_title || state.session.workspace_slug || wt("Keiner", "None"), detail: wt("Aktueller Arbeitsbereich", "Current workspace") },
      { label: wt("Konto", "Account"), value: state.session.user?.email || state.session.user_email || wt("Unbekannt", "Unknown"), detail: wt("Gerade angemeldet", "Currently signed in") },
    ];
    if (isMemberOnly()) {
      setStateCard(dom.complianceStateCard, wt("Hier stehen nur eigene Datenschutz-Aktionen bereit.", "Only self-service compliance actions are available here."), wt("Dieses Konto kann eigene Exporte anfordern und Löschanfragen starten, aber keine teamweiten Regeln verwalten.", "This account can request its own exports and erasure actions, but cannot manage team-wide rules."), "warning");
      renderMetricGrid(dom.complianceMetricGrid, selfServiceCards);
      [dom.complianceRetentionList, dom.complianceHoldList, dom.complianceErasureList, dom.complianceExportList, dom.complianceInventoryList, dom.regulatedCandidateList, dom.regulatedDocumentWorkbenchList].forEach((element) =>
        renderList(element, [], () => "", wt("Für diese Liste ist eine Freigabe durch Verwaltung nötig.", "An admin review is required for this list."))
      );
      return;
    }
    const [retentionPayload, holdsPayload, erasuresPayload, exportsPayload, inventoryPayload, candidatePayload, regulatedPayload] = await Promise.all([
      fetchJson("/compliance/retention-policies"),
      fetchJson("/compliance/legal-holds"),
      fetchJson("/compliance/erasure-requests"),
      fetchJson("/compliance/data-exports"),
      fetchJson("/compliance/data-inventory"),
      fetchJson("/compliance/regulated-documents/candidates"),
      fetchJson("/compliance/regulated-documents"),
    ]);
    const policies = retentionPayload.items || retentionPayload.policies || [];
    const holds = holdsPayload.items || holdsPayload.legal_holds || [];
    const erasures = erasuresPayload.items || erasuresPayload.erasure_requests || [];
    const exports = exportsPayload.items || exportsPayload.data_exports || [];
    const inventory = inventoryPayload.item || inventoryPayload.inventory || {};
    const candidates = candidatePayload.items || candidatePayload.regulated_document_candidates || [];
    const regulated = regulatedPayload.items || regulatedPayload.regulated_documents || [];
    renderMetricGrid(dom.complianceMetricGrid, [
      { label: wt("Aufbewahrung", "Retention"), value: String(policies.length), detail: wt("Regeln je Datentyp", "Rules by data type") },
      { label: wt("Sperren", "Legal holds"), value: String(holds.filter((item) => item.active).length), detail: wt("Gerade aktiv", "Currently active") },
      { label: wt("Löschanfragen", "Erasure queue"), value: String(erasures.length), detail: wt("Warten auf Prüfung", "Waiting for review") },
      { label: wt("Exporte", "Exports"), value: String(exports.length), detail: wt("Nachweisfähige Vorgänge", "Evidence-ready jobs") },
    ]);
    renderList(dom.complianceRetentionList, policies, (policy) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(prettyDataClass(policy.data_class))}</strong>
          <span class="workbench-pill" data-tone="${policy.legal_hold_enabled ? "warning" : "success"}">${policy.retention_days} ${wt("Tage", "days")}</span>
        </div>
        ${rowMeta([policy.legal_hold_enabled ? wt("Mit Sperrregel verknüpft", "Can be blocked by a legal hold") : wt("Ohne Sperrregel", "No legal-hold trigger"), formatDate(policy.updated_at)])}
      </li>
    `, wt("Noch keine Aufbewahrungsregeln eingerichtet.", "No retention rules are set up yet."));
    renderList(dom.complianceHoldList, holds, (hold) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(hold.reason || wt("Sperrvermerk", "Legal hold"))}</strong>
          <span class="workbench-pill" data-tone="${hold.active ? "warning" : "success"}">${hold.active ? prettyStatus("active") : prettyStatus("released")}</span>
        </div>
        ${rowMeta([hold.workspace_slug, hold.target_user_id, formatDate(hold.created_at)])}
      </li>
    `, wt("Zurzeit sind keine Sperrvermerke aktiv.", "No legal holds are active right now."));
    renderList(dom.complianceErasureList, erasures, (request) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(request.target_user_id)}</strong>
          <span class="workbench-pill" data-tone="${request.status === "completed" ? "success" : request.status === "blocked" ? "danger" : "warning"}">${escapeHTML(prettyStatus(request.status || "requested"))}</span>
        </div>
        ${rowMeta([request.workspace_slug, humanizeToken(request.legal_hold_decision), humanizeToken(request.retention_decision), formatDate(request.updated_at)])}
        ${request.status === "requested" ? actionButtons([{ type: "execute-erasure", label: wt("Jetzt ausführen", "Run now"), primary: true, dataset: { requestId: request.id } }]) : ""}
      </li>
    `, wt("Es gibt gerade keine offenen Löschanfragen.", "There are no erasure requests in the queue."));
    renderList(dom.complianceExportList, exports, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.workspace_slug || item.target_user_id || item.id)}</strong>
          <span class="workbench-pill" data-tone="${item.status === "completed" ? "success" : "warning"}">${escapeHTML(prettyStatus(item.status || "requested"))}</span>
        </div>
        ${rowMeta([item.workspace_slug ? wt("Export für Arbeitsbereich", "Workspace export") : wt("Export für Person", "Subject export"), formatDate(item.updated_at)])}
        ${actionButtons([{ type: "inspect-export", label: wt("Ansehen", "Open details"), dataset: { exportId: item.id } }])}
      </li>
    `, wt("Es wurden noch keine Exporte erzeugt.", "No export jobs have been created yet."));
    renderList(dom.regulatedCandidateList, candidates, (candidate) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(candidate.title || candidate.id)}</strong>
          <span class="workbench-pill" data-tone="warning">${escapeHTML(prettyDataClass(candidate.data_class || "regulated_business"))}</span>
        </div>
        ${rowMeta([humanizeToken(candidate.category), humanizeToken(candidate.retention_state), formatDate(candidate.updated_at)])}
        ${actionButtons([{ type: "finalize-regulated", label: wt("Final übernehmen", "Finalize"), primary: true, dataset: { documentId: candidate.id, title: candidate.title || candidate.id } }])}
      </li>
    `, wt("Zurzeit warten keine Dokumente auf die finale Einstufung.", "No regulated-document candidates are waiting."));
    renderList(dom.regulatedDocumentWorkbenchList, regulated, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.title)}</strong>
          <span class="workbench-pill" data-tone="${item.immutability_state === "finalized" ? "success" : "warning"}">${escapeHTML(humanizeToken(item.immutability_state))}</span>
        </div>
        ${rowMeta([humanizeToken(item.retention_state), item.current_version_number ? `${wt("Version", "Version")} ${item.current_version_number}` : null, formatDate(item.finalized_at || item.updated_at)])}
      </li>
    `, wt("Noch keine regelgebundenen Dokumente finalisiert.", "No regulated documents have been finalized yet."));
    renderList(dom.complianceInventoryList, Object.entries(inventory), ([name, info]) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(humanizeToken(name))}</strong>
          <span class="workbench-pill" data-tone="${info.erasable ? "success" : "warning"}">${prettyDataClass(info.data_class || "unclassified")}</span>
        </div>
        ${rowMeta([info.exportable ? prettyDecision("exportable") : prettyDecision("not_exportable"), info.erasable ? prettyDecision("erasable") : prettyDecision("retention_bound"), info.pseudonymize_only ? prettyDecision("pseudonymize_only") : null])}
      </li>
    `, wt("Es wurden keine Datenklassen zurückgegeben.", "No inventory records were returned."));
  }

  async function renderIntelligence() {
    const governanceRole = hasRole("org_owner", "org_admin");
    const [workbenchPayload, candidatePayload, examplesPayload, exportsPayload, venomPayload] = await Promise.all([
      fetchJson("/intelligence/workbench"),
      governanceRole ? fetchJson("/intelligence/promotion-candidates") : Promise.resolve({ items: [] }),
      governanceRole ? fetchJson("/intelligence/training-examples") : Promise.resolve({ items: [] }),
      governanceRole ? fetchJson("/intelligence/training-exports") : Promise.resolve({ items: [] }),
      fetchJson("/api/venom/stats").catch(() => ({})),
    ]);
    const workbench = workbenchPayload.item || workbenchPayload.workbench || {};
    const worldState = workbench.world_state || {};
    const recommendations = workbench.recommendations || [];
    const focusHints = workbench.focus_hints || [];
    const decisions = workbench.decisions || [];
    const obligations = worldState.obligations || [];
    const candidates = candidatePayload.items || candidatePayload.promotion_candidates || [];
    const examples = examplesPayload.items || examplesPayload.training_examples || [];
    const exports = exportsPayload.items || exportsPayload.training_exports || [];
    const venomStats = venomPayload || {};
    renderMetricGrid(dom.intelligenceMetricGrid, [
      { label: wt("Bereite Aufgaben", "Prepared work"), value: String(recommendations.length), detail: wt("Nach lokaler Relevanz sortiert", "Ranked by local relevance") },
      { label: wt("Fehlende Punkte", "Missing pieces"), value: String(obligations.length), detail: wt("Blockiert oder wartet auf Infos", "Blocked or waiting on input") },
      { label: wt("Hinweise", "Focus hints"), value: String(focusHints.length), detail: wt(`${worldState.risk_count || 0} kritisch oder blockiert`, `${worldState.risk_count || 0} critical or blocked`) },
      {
        label: wt("Muster", "Patterns"),
        value: String(venomStats.total_patterns || 0),
        detail: venomStats.promoted_patterns != null
          ? wt(`${venomStats.promoted_patterns} übernommen / ${venomStats.llm_calls_saved || 0} Aufrufe gespart`, `${venomStats.promoted_patterns} promoted / ${venomStats.llm_calls_saved || 0} calls saved`)
          : (governanceRole ? wt("Noch keine Musterstatistik", "No pattern stats available") : wt("Lokale Hinweisdaten", "Local advisory stats")),
      },
    ]);
    setStateCard(
      dom.intelligenceStateCard,
      recommendations[0]?.title || wt("Eine Aufgabe ist bereit zur Prüfung.", "Prepared work is ready to review."),
      recommendations[0]
        ? wt(
            `${prettyStatus(recommendations[0].readiness_status || "ready_now")} · ${humanizeToken(recommendations[0].recommendation_type || "prepared_work")} · Relevanz ${recommendations[0].ranking_explanation?.score || 0}`,
            `${prettyStatus(recommendations[0].readiness_status || "ready_now")} · ${humanizeToken(recommendations[0].recommendation_type || "prepared_work")} · Score ${recommendations[0].ranking_explanation?.score || 0}`
          )
        : governanceRole
          ? wt("Gerade liegt nichts über der lokalen Relevanzschwelle.", "Nothing is currently ranked above the local relevance threshold.")
          : wt("KERN bereitet hier arbeitsfertigen Kontext vor. Gemeinsame Freigaben bleiben bewusst manuell.", "KERN prepares worker-ready context here. Shared review stays manual by design."),
      recommendations[0]?.readiness_status === "blocked" || recommendations[0]?.risk_level === "high" ? "warning" : "success"
    );
    renderList(dom.recommendationList, recommendations, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(translateWorkbenchPayloadText(item.title || item.recommendation_type || item.id))}</strong>
          <span class="workbench-pill" data-tone="${item.readiness_status === "blocked" ? "danger" : item.readiness_status === "waiting_on_input" ? "warning" : "success"}">${escapeHTML(prettyStatus(item.readiness_status || "ready_now"))}</span>
        </div>
        ${rowMeta([
          item.workflow_type ? humanizeToken(item.workflow_type) : null,
          item.evidence_bundle?.scope ? prettyScope(item.evidence_bundle.scope) : null,
          item.preparation_scope ? wt(`Vorbereitung: ${prettyScope(item.preparation_scope)}`, `Prep: ${prettyScope(item.preparation_scope)}`) : null,
          item.ranking_explanation?.score != null ? wt(`Relevanz ${item.ranking_explanation.score}`, `Score ${item.ranking_explanation.score}`) : null,
        ])}
        <p class="panel-state__body">${escapeHTML(item.reason || wt("Noch keine kurze Begründung vorhanden.", "No short explanation is available yet."))}</p>
        ${actionButtons([
          { type: "inspect-preparation", label: wt("Details ansehen", "Open details"), primary: true, dataset: { recommendationId: item.id } },
          ...(item.recommendation_type === "suggested_draft" || item.recommendation_type === "follow_up_candidate"
            ? [{ type: "draft-preparation", label: wt("Entwurf erstellen", "Draft wording"), dataset: { recommendationId: item.id } }]
            : []),
          { type: "keep-personal", label: wt("Nur für mich", "Keep personal"), dataset: { recommendationId: item.id } },
          { type: "promote-preparation", label: wt("Zur Prüfung weitergeben", "Send for review"), dataset: { recommendationId: item.id } },
          ...(item.missing_inputs?.length ? [{ type: "ask-missing-info", label: wt("Fehlende Infos anfragen", "Ask for missing info"), dataset: { recommendationId: item.id } }] : []),
          { type: "mark-not-relevant", label: wt("Nicht relevant", "Not relevant"), dataset: { recommendationId: item.id } },
        ])}
      </li>
    `, wt("Zurzeit liegt keine vorbereitete Aufgabe vor.", "No prepared work is available right now."));
    const missingItems = [
      ...recommendations.flatMap((item) => (item.missing_inputs || []).map((missing) => ({ ...missing, recommendationId: item.id, packetTitle: item.title }))),
      ...obligations.map((item) => ({ id: item.id, label: item.title, reason: item.reason, severity: item.status === "blocked" ? "blocking" : "warning", recommendationId: item.workflow_id, packetTitle: item.title })),
    ];
    renderList(dom.obligationList, missingItems, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(translateWorkbenchPayloadText(item.label || item.packetTitle || item.id))}</strong>
          <span class="workbench-pill" data-tone="${item.severity === "blocking" ? "danger" : item.severity === "warning" ? "warning" : "success"}">${escapeHTML(prettyStatus(item.severity || "info"))}</span>
        </div>
        ${rowMeta([
          item.packetTitle && item.packetTitle !== item.label ? item.packetTitle : null,
          item.recommendationId ? wt(`Aus Aufgabe ${item.recommendationId}`, `From task ${item.recommendationId}`) : null,
        ])}
        <p class="panel-state__body">${escapeHTML(item.reason || wt("Es wurde noch kein Grund hinterlegt.", "No reason has been recorded yet."))}</p>
      </li>
    `, wt("Es gibt gerade keine offenen Voraussetzungen.", "There are no active missing prerequisites."));
    renderList(dom.workflowList, focusHints, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(translateWorkbenchPayloadText(item.title || item.id))}</strong>
          <span class="workbench-pill" data-tone="${item.readiness_status === "blocked" ? "danger" : item.readiness_status === "waiting_on_input" ? "warning" : "success"}">${escapeHTML(prettyStatus(item.readiness_status || "ready_now"))}</span>
        </div>
        ${rowMeta([
          item.workflow_id,
          item.score != null ? wt(`Relevanz ${item.score}`, `Score ${item.score}`) : null,
          item.risk_level ? wt(`Risiko ${humanizeToken(item.risk_level)}`, `Risk ${humanizeToken(item.risk_level)}`) : null,
        ])}
        <p class="panel-state__body">${escapeHTML((item.why_now || []).join(" ") || wt("Noch keine kurze Erklärung vorhanden.", "No short explanation is available yet."))}</p>
        ${actionButtons(item.recommendation_id ? [{ type: "inspect-preparation", label: wt("Details ansehen", "Open details"), dataset: { recommendationId: item.recommendation_id } }] : [])}
      </li>
    `, wt("Momentan gibt es keine Fokus-Hinweise.", "No focus hints are active right now."));
    renderList(dom.decisionHistoryList, decisions, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(humanizeToken(item.decision_kind || wt("Entscheidung", "decision")))}</strong>
          <span class="workbench-pill" data-tone="success">${escapeHTML(humanizeToken(item.decision_value || wt("gespeichert", "recorded")))}</span>
        </div>
        ${rowMeta([humanizeToken(item.source_type), item.source_id, formatDate(item.created_at)])}
        <p class="panel-state__body">${escapeHTML(item.rationale || wt("Es wurde noch keine Begründung gespeichert.", "No rationale has been recorded yet."))}</p>
      </li>
    `, wt("Es gibt noch keine gespeicherten Entscheidungen.", "No decision history has been recorded yet."));
    renderList(dom.promotionCandidatesList, candidates, (candidate) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(candidate.summary || candidate.value || candidate.key || candidate.id)}</strong>
          <span class="workbench-pill" data-tone="${candidate.provenance?.policy_safe ? "success" : "warning"}">${escapeHTML(prettyScope(candidate.provenance?.scope || candidate.scope || "workspace"))}</span>
        </div>
        ${rowMeta([
          candidate.provenance?.workspace_slug,
          candidate.provenance?.policy_safe === false ? wt("Manuelle Prüfung nötig", "Manual review needed") : wt("Regelkonform", "Policy-safe"),
          candidate.ranking_explanation?.prior_approvals != null ? wt(`${candidate.ranking_explanation.prior_approvals} frühere Freigaben`, `${candidate.ranking_explanation.prior_approvals} prior approvals`) : null,
        ])}
        ${actionButtons([
          { type: "review-promotion", label: wt("Freigeben", "Approve"), primary: true, dataset: { memoryId: candidate.id, decision: "approved" } },
          { type: "review-promotion", label: wt("Nur persönlich", "Personal only"), dataset: { memoryId: candidate.id, decision: "personal_only" } },
          { type: "review-promotion", label: wt("Ablehnen", "Reject"), dataset: { memoryId: candidate.id, decision: "rejected" } },
        ])}
      </li>
    `, governanceRole ? wt("Es warten keine Inhalte auf eine Freigabe.", "No promotion candidates are waiting.") : wt("Diese Freigabe ist nur für Governance-Rollen sichtbar.", "Promotion review is restricted to governance roles."));
    renderList(dom.trainingExamplesList, examples, (example) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(`${humanizeToken(example.source_type)} · ${example.source_id}`)}</strong>
          <span class="workbench-pill" data-tone="${example.status === "approved" ? "success" : example.status === "rejected" ? "danger" : "warning"}">${escapeHTML(prettyStatus(example.status))}</span>
        </div>
        ${rowMeta([example.workspace_slug, prettyDataClass(example.metadata?.data_class), example.approved_for_training ? wt("Für Training freigegeben", "Approved for training") : wt("Noch nicht freigegeben", "Not approved yet")])}
        ${actionButtons([
          { type: "review-example", label: wt("Freigeben", "Approve"), primary: true, dataset: { exampleId: example.id, status: "approved" } },
          { type: "review-example", label: wt("Ablehnen", "Reject"), dataset: { exampleId: example.id, status: "rejected" } },
        ])}
      </li>
    `, governanceRole ? wt("Es stehen keine Trainingsbeispiele an.", "No training examples are queued.") : wt("Diese Prüfung ist nur für Governance-Rollen sichtbar.", "Training example review is restricted to governance roles."));
    renderList(dom.trainingExportsList, exports, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.id || item.workspace_slug || wt("Trainingspaket", "Training export"))}</strong>
          <span class="workbench-pill" data-tone="success">${escapeHTML(item.train_count != null ? wt(`${item.train_count} Training`, `${item.train_count} train`) : prettyStatus("ready_now"))}</span>
        </div>
        ${rowMeta([
          item.workspace_slug,
          item.validation_count != null ? wt(`${item.validation_count} zur Prüfung`, `${item.validation_count} validation`) : null,
          item.compliance_filter_report ? wt(`${item.compliance_filter_report.excluded_personal || 0} persönliche Inhalte entfernt`, `${item.compliance_filter_report.excluded_personal || 0} personal items filtered`) : null,
        ])}
        ${actionButtons([{ type: "inspect-training-export", label: wt("Ansehen", "Open details"), dataset: { exportId: item.id } }])}
      </li>
    `, governanceRole ? wt("Es wurden noch keine Trainingspakete erzeugt.", "No training exports have been generated yet.") : wt("Diese Ansicht ist nur für Governance-Rollen sichtbar.", "Training export review is restricted to governance roles."));
  }

  async function renderEvidence() {
    const [health, exportPayload, trainingPayload] = await Promise.all([
      fetchJson("/health"),
      hasRole("org_owner", "org_admin", "auditor") ? fetchJson("/compliance/data-exports") : Promise.resolve({ items: [] }),
      hasRole("org_owner", "org_admin", "auditor") ? fetchJson("/intelligence/training-exports") : Promise.resolve({ items: [] }),
    ]);
    const exports = exportPayload.items || exportPayload.data_exports || [];
    const trainingExports = trainingPayload.items || trainingPayload.training_exports || [];
    renderMetricGrid(dom.evidenceMetricGrid, [
      { label: wt("System", "Health"), value: humanizeToken(health.status || wt("unbekannt", "unknown")), detail: wt(`Seit ${health.uptime_seconds || 0}s aktiv`, `${health.uptime_seconds || 0}s uptime`) },
      { label: wt("Prüfpfad", "Audit chain"), value: health.audit_chain_ok ? wt("In Ordnung", "Healthy") : wt("Prüfen", "Review"), detail: health.profile_locked ? wt("Arbeitsbereich ist gesperrt", "Workspace is locked") : wt("Arbeitsbereich ist entsperrt", "Workspace is unlocked") },
      { label: wt("Nachweis-Exporte", "Compliance exports"), value: String(exports.length), detail: wt("Bereit zum Nachvollziehen", "Ready for traceability") },
      { label: wt("Trainingspakete", "Training exports"), value: String(trainingExports.length), detail: wt("Offline bereitgestellt", "Prepared offline") },
    ]);
    renderList(dom.evidenceManifestList, exports, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.workspace_slug || item.target_user_id || item.id)}</strong>
          <span class="workbench-pill" data-tone="${item.status === "completed" ? "success" : "warning"}">${escapeHTML(prettyStatus(item.status || "requested"))}</span>
        </div>
        ${rowMeta([item.workspace_slug ? wt("Export für Arbeitsbereich", "Workspace export") : wt("Export für Person", "Subject export"), formatDate(item.updated_at)])}
        ${actionButtons([{ type: "inspect-export", label: wt("Ansehen", "Open details"), dataset: { exportId: item.id } }])}
      </li>
    `, wt("Es sind noch keine Nachweis-Exporte verfügbar.", "No compliance exports are available."));
    renderList(dom.evidenceTrainingManifestList, trainingExports, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.id || item.workspace_slug || wt("Trainingspaket", "Training export"))}</strong>
          <span class="workbench-pill" data-tone="success">${escapeHTML(item.train_count != null ? wt(`${item.train_count} Training`, `${item.train_count} train`) : prettyStatus("ready_now"))}</span>
        </div>
        ${rowMeta([item.workspace_slug, item.validation_count != null ? wt(`${item.validation_count} zur Prüfung`, `${item.validation_count} validation`) : null, formatDate(item.created_at)])}
        ${actionButtons([{ type: "inspect-training-export", label: wt("Ansehen", "Open details"), dataset: { exportId: item.id } }])}
      </li>
    `, wt("Es sind noch keine Trainingspakete verfügbar.", "No training dataset manifests are available."));
  }

  async function renderTab(tabName) {
    state.activeTab = tabName;
    try {
      if (tabName === "workspace") await renderWorkspace();
      if (tabName === "admin") await renderAdmin();
      if (tabName === "compliance") await renderCompliance();
      if (tabName === "intelligence") await renderIntelligence();
      if (tabName === "evidence") await renderEvidence();
    } catch (error) {
      const target = {
        workspace: dom.workspaceSummaryState,
        admin: dom.adminAccessState,
        compliance: dom.complianceStateCard,
        intelligence: dom.intelligenceStateCard,
        evidence: dom.evidenceStateCard,
      }[tabName] || dom.workspaceSummaryState;
      setStateCard(
        target,
        error.status === 403 ? wt("Dieser Bereich ist im aktuellen Arbeitsbereich nicht freigegeben.", "This area is not available in the current workspace.") : wt("Dieser Bereich konnte nicht geladen werden.", "This page could not be loaded."),
        error.message || wt("KERN konnte diese Ansicht gerade nicht öffnen.", "KERN could not open this view right now."),
        "error"
      );
    }
  }

  async function runAction(action, dataset) {
    if (action === "approve-user") {
      await fetchJson(`/admin/users/${dataset.userId}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_slug: dom.adminUserWorkspace?.value || state.session.workspace_slug, role: "member" }),
      });
      await renderAdmin();
      return;
    }
    if (action === "suspend-user") {
      await fetchJson(`/admin/users/${dataset.userId}/suspend`, { method: "POST" });
      await renderAdmin();
      return;
    }
    if (action === "revoke-session") {
      await fetchJson(`/admin/sessions/${dataset.sessionId}/revoke`, { method: "POST" });
      await renderAdmin();
      return;
    }
    if (action === "execute-erasure") {
      await fetchJson(`/compliance/erasure-requests/${dataset.requestId}/execute`, { method: "POST" });
      await renderCompliance();
      return;
    }
    if (action === "finalize-regulated") {
      await fetchJson("/compliance/regulated-documents/finalize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_id: dataset.documentId, title: dataset.title }),
      });
      await renderCompliance();
      return;
    }
    if (action === "review-promotion") {
      await fetchJson(`/intelligence/promotion-candidates/${dataset.memoryId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: dataset.decision }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "review-example") {
      await fetchJson(`/intelligence/training-examples/${dataset.exampleId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: dataset.status }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "inspect-export") {
      const detail = await fetchJson(`/compliance/data-exports/${dataset.exportId}`);
      const exportStatus = prettyStatus(detail.item?.status || "unknown");
      const exportTime = detail.manifest?.created_at || detail.item?.updated_at
        ? formatDate(detail.manifest?.created_at || detail.item?.updated_at)
        : wt("Noch kein Zeitstempel", "No timestamp yet");
      const exportFile = detail.artifact?.path
        ? shortPath(detail.artifact.path)
        : wt("Datei wird noch erstellt", "File is still being prepared");
      setStateCard(
        dom.evidenceStateCard,
        detail.item?.workspace_slug || detail.item?.target_user_id || wt("Exportdetails", "Export details"),
        wt(
          `${exportStatus} · ${exportTime} · Datei ${exportFile}`,
          `${exportStatus} · ${exportTime} · File ${exportFile}`
        ),
        detail.item?.status === "completed" ? "success" : "warning"
      );
      return;
    }
    if (action === "inspect-training-export") {
      const detail = await fetchJson(`/intelligence/training-exports/${dataset.exportId}`);
      const manifest = detail.manifest || {};
      setStateCard(
        dom.intelligenceStateCard,
        detail.item?.id || dataset.exportId,
        wt(
          `${manifest.train_count || 0} Trainingsbeispiele · ${manifest.validation_count || 0} zur Prüfung · ${manifest.dedup_count || 0} Dubletten entfernt · ${manifest.compliance_filter_report?.excluded_personal || 0} persönliche Inhalte ausgeschlossen`,
          `${manifest.train_count || 0} training examples · ${manifest.validation_count || 0} for review · ${manifest.dedup_count || 0} duplicates removed · ${manifest.compliance_filter_report?.excluded_personal || 0} personal items excluded`
        ),
        "success"
      );
      return;
    }
    if (action === "inspect-preparation") {
      const detail = await fetchJson(`/intelligence/preparation/${dataset.recommendationId}`);
      const packet = detail.item || detail.preparation_packet || {};
      const claims = packet.evidence_pack?.claims || [];
      const missing = packet.missing_inputs || [];
      const events = packet.event_refs || packet.evidence_pack?.event_refs || [];
      setStateCard(
        dom.intelligenceStateCard,
        packet.title || dataset.recommendationId,
        wt(
          `${packet.summary || "Noch keine Zusammenfassung vorhanden."} · ${prettyStatus(packet.readiness_status || "ready_now")} · ${missing.length} fehlende Angaben · ${claims.length} belegte Punkte · ${events.length} Aktivitäten · ${prettyScope(packet.preparation_scope || packet.evidence_pack?.scope || "workspace")}`,
          `${packet.summary || "No summary yet."} · ${prettyStatus(packet.readiness_status || "ready_now")} · ${missing.length} missing inputs · ${claims.length} supported points · ${events.length} activity references · ${prettyScope(packet.preparation_scope || packet.evidence_pack?.scope || "workspace")}`
        ),
        packet.readiness_status === "blocked" ? "warning" : "success"
      );
      return;
    }
    if (action === "draft-preparation") {
      const detail = await fetchJson(`/intelligence/preparation/${dataset.recommendationId}/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "llm_rewrite" }),
      });
      const draft = detail.item || detail.suggested_draft || {};
      setStateCard(
        dom.intelligenceStateCard,
        draft.subject || draft.title || wt("Vorbereiteter Entwurf", "Prepared draft"),
        wt(
          `${wt("Modus", "Mode")} ${humanizeToken(detail.render_mode || draft.mode || "deterministic_scaffold")} · ${(draft.body || "Noch kein Text erzeugt.")}`.slice(0, 320),
          `${wt("Modus", "Mode")} ${humanizeToken(detail.render_mode || draft.mode || "deterministic_scaffold")} · ${(draft.body || "No text generated yet.")}`.slice(0, 320)
        ),
        "success"
      );
      return;
    }
    if (action === "keep-personal") {
      await fetchJson("/intelligence/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          signal_type: "personal_preference",
          source_type: "preparation",
          source_id: dataset.recommendationId,
          metadata: { action: "keep_personal" },
        }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "promote-preparation") {
      await fetchJson("/intelligence/memory/promote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          signal_type: "promote_workspace",
          source_type: "preparation",
          source_id: dataset.recommendationId,
          metadata: { action: "promote_for_review" },
        }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "ask-missing-info") {
      const detail = await fetchJson(`/intelligence/preparation/${dataset.recommendationId}`);
      const packet = detail.item || detail.preparation_packet || {};
      const prompts = (packet.missing_inputs || []).map((item) => `${item.label}: ${item.reason}`);
      setStateCard(
        dom.intelligenceStateCard,
        packet.title || dataset.recommendationId,
        prompts.length
          ? wt(`Bitte noch klären: ${prompts.join(" | ")}`, `Still needed: ${prompts.join(" | ")}`)
          : wt("Aktuell fehlen keine weiteren Angaben.", "No additional information is needed right now."),
        prompts.length ? "warning" : "success"
      );
      return;
    }
    if (action === "mark-not-relevant") {
      await fetchJson("/intelligence/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          signal_type: "reject_pattern",
          source_type: "preparation",
          source_id: dataset.recommendationId,
          metadata: { action: "mark_not_relevant" },
        }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "inspect-workflow") {
      const detail = await fetchJson(`/intelligence/workflows/${dataset.workflowId}`);
      const workflow = detail.item || detail.workflow || {};
      setStateCard(
        dom.intelligenceStateCard,
        workflow.workflow_type || dataset.workflowId,
        `${workflow.last_event || "No event"} · next ${workflow.next_expected_step || "none"} · ${detail.events?.length || 0} recorded event(s)`,
        workflow.status === "blocked" ? "warning" : "success"
      );
    }
  }

  function bindEvents() {
    document.getElementById("workspaceRefreshButton")?.addEventListener("click", () => renderWorkspace());
    document.getElementById("adminRefreshButton")?.addEventListener("click", () => renderAdmin());
    document.getElementById("adminCreateWorkspaceButton")?.addEventListener("click", async () => {
      await fetchJson("/admin/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: dom.adminWorkspaceSlug?.value || "", title: dom.adminWorkspaceTitle?.value || "" }),
      });
      if (dom.adminWorkspaceSlug) dom.adminWorkspaceSlug.value = "";
      if (dom.adminWorkspaceTitle) dom.adminWorkspaceTitle.value = "";
      await renderAdmin();
      await renderWorkspace();
    });
    document.getElementById("adminCreateUserButton")?.addEventListener("click", async () => {
      await fetchJson("/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: dom.adminUserEmail?.value || "",
          display_name: dom.adminUserDisplayName?.value || "",
          role: dom.adminUserRole?.value || "member",
          workspace_slug: dom.adminUserWorkspace?.value || state.session.workspace_slug,
        }),
      });
      if (dom.adminUserEmail) dom.adminUserEmail.value = "";
      if (dom.adminUserDisplayName) dom.adminUserDisplayName.value = "";
      await renderAdmin();
    });
    document.getElementById("generateMyExportButton")?.addEventListener("click", async () => {
      if (!state.session?.user?.id) return;
      await fetchJson(`/compliance/exports/user/${state.session.user.id}/generate`, { method: "POST" });
      await renderCompliance();
      await renderEvidence();
    });
    document.getElementById("createSelfErasureButton")?.addEventListener("click", async () => {
      await fetchJson("/compliance/erasure-requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_user_id: state.session?.user?.id,
          workspace_slug: state.session?.workspace_slug,
          reason: dom.selfErasureReason?.value || "",
        }),
      });
      if (dom.selfErasureReason) dom.selfErasureReason.value = "";
      await renderCompliance();
    });
    document.getElementById("saveRetentionPolicyButton")?.addEventListener("click", async () => {
      await fetchJson("/compliance/retention-policies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          data_class: dom.retentionDataClass?.value,
          retention_days: Number(dom.retentionDays?.value || 0),
          legal_hold_enabled: !!dom.retentionLegalHoldEnabled?.checked,
        }),
      });
      await renderCompliance();
    });
    document.getElementById("createLegalHoldButton")?.addEventListener("click", async () => {
      await fetchJson("/compliance/legal-holds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace_slug: dom.legalHoldWorkspace?.value || state.session?.workspace_slug,
          reason: dom.legalHoldReason?.value || "",
        }),
      });
      if (dom.legalHoldReason) dom.legalHoldReason.value = "";
      await renderCompliance();
    });
    document.getElementById("generateWorkspaceExportButton")?.addEventListener("click", async () => {
      await fetchJson(`/compliance/exports/workspace/${state.session?.workspace_slug}/generate`, { method: "POST" });
      await renderCompliance();
      await renderEvidence();
    });
    document.getElementById("createTrainingExportButton")?.addEventListener("click", async () => {
      await fetchJson("/intelligence/training-exports", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_slug: state.session?.workspace_slug }),
      });
      await renderIntelligence();
      await renderEvidence();
    });
    dom.modal?.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-workbench-action]");
      if (!button) return;
      await runAction(button.dataset.workbenchAction, button.dataset);
    });
  }

  return {
    async init() {
      await refreshSession();
      bindEvents();
      await renderWorkspace();
    },
    async onTabActivated(tabName) {
      await renderTab(tabName);
    },
    async refreshActiveTab() {
      await renderTab(state.activeTab);
    },
  };
}
